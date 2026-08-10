"""Fail-closed SQLAlchemy engine and session lifecycle management."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker


EXPECTED_DATABASE_REVISION = "20260809_0007"


class DatabasePhase(StrEnum):
    NOT_STARTED = "not_started"
    STARTING = "starting"
    DISABLED = "disabled"
    READY = "ready"
    FAILED = "failed"
    CLOSED = "closed"


class DatabaseInitializationError(RuntimeError):
    """Database lifecycle error safe to expose through a readiness endpoint."""

    def __init__(self, code: str, public_message: str) -> None:
        self.code = code
        self.public_message = public_message
        super().__init__(public_message)


@dataclass(frozen=True, slots=True)
class DatabaseFailure:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class DatabaseReadiness:
    ready: bool
    phase: DatabasePhase
    code: str
    message: str


EngineFactory = Callable[..., Engine]
SessionFactory = sessionmaker[Session]


class DatabaseManager:
    """Own exactly one process-local engine and its session factory.

    ``start`` probes connectivity and, when configured, the Alembic revision.
    Schema creation and migration remain explicit Alembic operations and are
    never performed by this class.
    """

    def __init__(
        self,
        database_url: str | None,
        *,
        engine_options: Mapping[str, Any] | None = None,
        engine_factory: EngineFactory = create_engine,
        expected_revision: str | None = None,
    ) -> None:
        self._database_url = (database_url or "").strip()
        self._engine_options = dict(engine_options or {})
        self._engine_factory = engine_factory
        self._expected_revision = expected_revision
        self._engine: Engine | None = None
        self._session_factory: SessionFactory | None = None
        self.phase = DatabasePhase.NOT_STARTED
        self.failure: DatabaseFailure | None = None

    def start(self) -> None:
        if self.phase != DatabasePhase.NOT_STARTED:
            raise RuntimeError("database manager may only be started once")
        if not self._database_url:
            self.phase = DatabasePhase.DISABLED
            self.failure = DatabaseFailure(
                "database_disabled",
                "Database persistence is not configured.",
            )
            return

        self.phase = DatabasePhase.STARTING
        options: dict[str, Any] = {
            "pool_pre_ping": True,
            "hide_parameters": True,
        }
        options.update(self._engine_options)
        engine: Engine | None = None
        try:
            engine = self._engine_factory(self._database_url, **options)
            if engine.dialect.name == "sqlite":
                event.listen(engine, "connect", _enable_sqlite_foreign_keys)
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception:
            self._dispose_failed_engine(engine)
            self._fail(
                "database_unavailable",
                "The configured database is unavailable.",
            )
            return
        assert engine is not None

        if self._expected_revision is not None:
            try:
                with engine.connect() as connection:
                    revisions = list(
                        connection.scalars(
                            text("SELECT version_num FROM alembic_version")
                        )
                    )
            except Exception:
                self._dispose_failed_engine(engine)
                self._fail(
                    "database_schema_not_ready",
                    "The Production database schema is not migrated.",
                )
                return
            if revisions != [self._expected_revision]:
                self._dispose_failed_engine(engine)
                self._fail(
                    "database_schema_not_ready",
                    "The Production database schema revision is incompatible.",
                )
                return

        try:
            factory = sessionmaker(
                bind=engine,
                class_=Session,
                autoflush=True,
                expire_on_commit=False,
            )
        except Exception:
            self._dispose_failed_engine(engine)
            self._fail(
                "database_unavailable",
                "The configured database is unavailable.",
            )
            return

        self._engine = engine
        self._session_factory = factory
        self.failure = None
        self.phase = DatabasePhase.READY

    def _dispose_failed_engine(self, engine: Engine | None) -> None:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
        self._engine = None
        self._session_factory = None

    def _fail(self, code: str, message: str) -> None:
        self.phase = DatabasePhase.FAILED
        self.failure = DatabaseFailure(code, message)

    def require_ready(self) -> SessionFactory:
        if self.phase == DatabasePhase.READY and self._session_factory is not None:
            return self._session_factory
        failure = self.failure or DatabaseFailure(
            "database_not_started",
            "Database initialization has not completed.",
        )
        raise DatabaseInitializationError(failure.code, failure.message)

    @property
    def session_factory(self) -> SessionFactory:
        return self.require_ready()

    @property
    def engine(self) -> Engine:
        self.require_ready()
        assert self._engine is not None
        return self._engine

    def readiness(self) -> DatabaseReadiness:
        if self.phase == DatabasePhase.READY:
            return DatabaseReadiness(
                ready=True,
                phase=self.phase,
                code="database_ready",
                message="Database persistence is ready.",
            )
        failure = self.failure or DatabaseFailure(
            "database_not_started",
            "Database initialization has not completed.",
        )
        return DatabaseReadiness(
            ready=False,
            phase=self.phase,
            code=failure.code,
            message=failure.message,
        )

    def close(self) -> None:
        if self.phase == DatabasePhase.CLOSED:
            return
        engine, self._engine = self._engine, None
        self._session_factory = None
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                # Shutdown is best effort and must never expose driver details.
                pass
        self.phase = DatabasePhase.CLOSED


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    """Make isolated SQLite unit tests preserve production FK semantics."""

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


__all__ = [
    "DatabaseFailure",
    "DatabaseInitializationError",
    "DatabaseManager",
    "DatabasePhase",
    "DatabaseReadiness",
    "EngineFactory",
    "EXPECTED_DATABASE_REVISION",
    "SessionFactory",
]
