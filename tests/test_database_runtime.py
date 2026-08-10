from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.database_runtime import (
    DatabaseRuntime,
    DatabaseRuntimeError,
    DatabaseRuntimePhase,
)
from app.main import create_app
from app.runtime import RuntimePhase
from knowledge.governance import MagicForgeMode


class _FakeDatabaseManager:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.start_calls = 0
        self.require_ready_calls = 0
        self.close_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def require_ready(self):
        self.require_ready_calls += 1
        if self.failure is not None:
            raise self.failure
        return object()

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_create_app_does_not_construct_or_open_database() -> None:
    factory_calls: list[tuple[str, float]] = []

    def forbidden_factory(database_url: str, timeout: float):
        factory_calls.append((database_url, timeout))
        raise AssertionError("database opened during create_app")

    application = create_app(
        settings=Settings(
            magicforge_mode=MagicForgeMode.PRODUCTION,
            database_url="postgresql+psycopg://user:secret@db/magicforge",
        ),
        database_manager_factory=forbidden_factory,
    )

    assert factory_calls == []
    assert application.state.database_runtime.phase == (
        DatabaseRuntimePhase.NOT_STARTED
    )


def test_bootstrap_database_is_disabled_without_configuration() -> None:
    def forbidden_factory(_database_url: str, _timeout: float):
        raise AssertionError("Bootstrap read runtime must not open PostgreSQL")

    runtime = DatabaseRuntime(
        database_url="",
        required=False,
        connect_timeout_seconds=5,
        manager_factory=forbidden_factory,
    )

    runtime.start()

    assert runtime.phase == DatabaseRuntimePhase.DISABLED
    assert runtime.application_ready is True
    runtime.require_application_ready()
    runtime.close()
    runtime.close()
    assert runtime.phase == DatabaseRuntimePhase.CLOSED


def test_configured_bootstrap_database_is_available_to_authentication() -> None:
    manager = _FakeDatabaseManager()
    runtime = DatabaseRuntime(
        database_url="postgresql+psycopg://magicforge:local@db/magicforge",
        required=False,
        connect_timeout_seconds=5,
        manager_factory=lambda _url, _timeout: manager,
    )

    runtime.start()

    assert runtime.phase == DatabaseRuntimePhase.READY
    assert runtime.require_database_ready() is not None
    assert manager.start_calls == 1
    assert manager.require_ready_calls == 2
    runtime.close()


def test_explicit_bootstrap_anonymous_mode_tolerates_optional_db_failure() -> None:
    manager = _FakeDatabaseManager(failure=RuntimeError("database unavailable"))
    runtime = DatabaseRuntime(
        database_url="postgresql+psycopg://magicforge:local@db/magicforge",
        required=False,
        allow_optional_failure=True,
        connect_timeout_seconds=5,
        manager_factory=lambda _url, _timeout: manager,
    )

    runtime.start()

    assert runtime.phase == DatabaseRuntimePhase.FAILED
    runtime.require_application_ready()
    with pytest.raises(DatabaseRuntimeError):
        runtime.require_database_ready()
    runtime.close()


@pytest.mark.anyio
async def test_production_without_database_is_live_but_not_ready() -> None:
    builder_calls = 0

    def runtime_builder(_settings):
        nonlocal builder_calls
        builder_calls += 1
        raise AssertionError("corpus runtime must not start before PostgreSQL")

    application = create_app(
        settings=Settings(magicforge_mode=MagicForgeMode.PRODUCTION),
        runtime_builder=runtime_builder,
    )

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")
            stats = await client.get("/stats")

    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert ready.status_code == 503
    assert ready.json() == {
        "detail": {
            "code": "database_not_configured",
            "message": "Production database configuration is missing.",
        }
    }
    assert stats.status_code == 401
    assert stats.json()["detail"]["code"] == "unauthenticated"
    assert builder_calls == 0
    assert application.state.runtime_container.phase == RuntimePhase.CLOSED


@pytest.mark.anyio
async def test_database_failure_is_safe_and_closes_partial_manager() -> None:
    secret = "database-password-must-not-leak"
    manager = _FakeDatabaseManager(
        failure=RuntimeError(f"could not connect with password {secret}")
    )
    builder_calls = 0

    def runtime_builder(_settings):
        nonlocal builder_calls
        builder_calls += 1
        raise AssertionError("corpus runtime must not start after DB failure")

    application = create_app(
        settings=Settings(
            magicforge_mode=MagicForgeMode.PRODUCTION,
            database_url=(
                f"postgresql+psycopg://magicforge:{secret}@db/magicforge"
            ),
        ),
        runtime_builder=runtime_builder,
        database_manager_factory=lambda _url, _timeout: manager,
    )

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")

    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json() == {
        "detail": {
            "code": "database_not_ready",
            "message": "The Production database is unavailable.",
        }
    }
    assert secret not in ready.text
    assert builder_calls == 0
    assert manager.start_calls == 1
    assert manager.require_ready_calls == 1
    assert manager.close_calls == 1


def test_ready_database_runtime_closes_manager_exactly_once() -> None:
    manager = _FakeDatabaseManager()
    runtime = DatabaseRuntime(
        database_url="postgresql+psycopg://magicforge:local@db/magicforge",
        required=True,
        connect_timeout_seconds=3.2,
        manager_factory=lambda _url, _timeout: manager,
    )

    runtime.start()

    assert runtime.phase == DatabaseRuntimePhase.READY
    assert manager.start_calls == 1
    assert manager.require_ready_calls == 1
    runtime.close()
    runtime.close()
    assert manager.close_calls == 1
