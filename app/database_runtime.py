"""Mode-aware ownership of the relational database lifecycle.

The database is mandatory in Production and optional in Bootstrap.  An
explicitly configured Bootstrap database is still opened so authentication can
be exercised locally; omitting it preserves the frozen anonymous read runtime.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy.engine import make_url


logger = logging.getLogger(__name__)


class ManagedDatabase(Protocol):
    """The lifecycle surface supplied by the persistence adapter."""

    def start(self) -> None: ...

    def require_ready(self): ...

    def close(self) -> None: ...


DatabaseManagerFactory = Callable[[str, float], ManagedDatabase]


class DatabaseRuntimePhase(StrEnum):
    NOT_STARTED = "not_started"
    DISABLED = "disabled"
    READY = "ready"
    FAILED = "failed"
    CLOSED = "closed"


class DatabaseRuntimeError(RuntimeError):
    """A database lifecycle failure with a credential-safe representation."""

    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


@dataclass(frozen=True, slots=True)
class DatabaseRuntimeFailure:
    code: str
    message: str


class DatabaseRuntime:
    """Open the Production database once, inside the FastAPI lifespan."""

    def __init__(
        self,
        *,
        database_url: str,
        required: bool,
        allow_optional_failure: bool = False,
        connect_timeout_seconds: float,
        manager_factory: DatabaseManagerFactory | None = None,
    ) -> None:
        self._database_url = database_url
        self.required = required
        self.allow_optional_failure = allow_optional_failure
        self.connect_timeout_seconds = connect_timeout_seconds
        self._manager_factory = manager_factory or _default_manager_factory
        self.phase = DatabaseRuntimePhase.NOT_STARTED
        self.failure: DatabaseRuntimeFailure | None = None
        self.manager: ManagedDatabase | None = None

    @property
    def application_ready(self) -> bool:
        return self.phase == DatabaseRuntimePhase.READY or (
            not self.required
            and (
                self.phase == DatabaseRuntimePhase.DISABLED
                or (
                    self.allow_optional_failure
                    and self.phase == DatabaseRuntimePhase.FAILED
                )
            )
        )

    def start(self) -> None:
        if self.phase != DatabaseRuntimePhase.NOT_STARTED:
            raise RuntimeError("database runtime may only be started once")

        if not self._database_url and not self.required:
            self.phase = DatabaseRuntimePhase.DISABLED
            return

        if not self._database_url:
            self._fail(
                "database_not_configured",
                "Production database configuration is missing.",
            )
            return

        try:
            parsed_url = make_url(self._database_url)
            if parsed_url.get_backend_name() != "postgresql":
                self._fail(
                    "database_backend_unsupported",
                    "Production persistence requires PostgreSQL.",
                )
                return
            manager = self._manager_factory(
                self._database_url,
                self.connect_timeout_seconds,
            )
            self.manager = manager
            manager.start()
            manager.require_ready()
        except Exception:
            # DBAPI errors may contain credentials or host details.  Never put
            # the underlying exception into logs or public readiness payloads.
            logger.error("Production database initialization failed safely")
            self._fail(
                "database_not_ready",
                "The Production database is unavailable.",
            )
            return

        self.phase = DatabaseRuntimePhase.READY

    def require_application_ready(self) -> None:
        if self.application_ready:
            return
        failure = self.failure or DatabaseRuntimeFailure(
            "database_not_started",
            "Database initialization has not completed.",
        )
        raise DatabaseRuntimeError(failure.code, failure.message)

    def require_database_ready(self):
        """Return a session factory or fail safely when persistence is absent."""

        if self.phase == DatabaseRuntimePhase.READY and self.manager is not None:
            try:
                return self.manager.require_ready()
            except Exception as exc:
                raise DatabaseRuntimeError(
                    "database_not_ready",
                    "The authentication database is unavailable.",
                ) from exc
        if self.phase == DatabaseRuntimePhase.DISABLED:
            raise DatabaseRuntimeError(
                "database_not_configured",
                "Authentication persistence is not configured.",
            )
        failure = self.failure or DatabaseRuntimeFailure(
            "database_not_started",
            "Database initialization has not completed.",
        )
        raise DatabaseRuntimeError(failure.code, failure.message)

    def close(self) -> None:
        if self.phase == DatabaseRuntimePhase.CLOSED:
            return
        if self.manager is not None:
            try:
                self.manager.close()
            except Exception:
                # Shutdown must remain credential-safe and idempotent.
                logger.error("Production database shutdown failed safely")
        self.phase = DatabaseRuntimePhase.CLOSED

    def _fail(self, code: str, message: str) -> None:
        self.phase = DatabaseRuntimePhase.FAILED
        self.failure = DatabaseRuntimeFailure(code, message)


def _default_manager_factory(
    database_url: str,
    connect_timeout_seconds: float,
) -> ManagedDatabase:
    from persistence.database import DatabaseManager, EXPECTED_DATABASE_REVISION

    timeout = max(1, math.ceil(connect_timeout_seconds))
    return DatabaseManager(
        database_url,
        engine_options={
            "pool_pre_ping": True,
            "connect_args": {"connect_timeout": timeout},
        },
        expected_revision=EXPECTED_DATABASE_REVISION,
    )
