"""Explicit SQLAlchemy transaction boundary for governance operations."""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session

from persistence.database import SessionFactory


class UnitOfWorkStateError(RuntimeError):
    """Raised when a unit of work is used outside its transaction scope."""


class SqlAlchemyUnitOfWork:
    """One explicit transaction per context manager entry.

    A successful context does not auto-commit: callers must call ``commit``
    after every required state change and AuditEvent have been staged.  An
    omitted commit therefore fails safe and rolls back.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        self._finished = False

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        if self._session is not None:
            raise UnitOfWorkStateError("unit of work is already active")
        self._session = self._session_factory()
        self._finished = False
        return self

    @property
    def session(self) -> Session:
        if self._session is None:
            raise UnitOfWorkStateError("unit of work is not active")
        return self._session

    def flush(self) -> None:
        self.session.flush()

    def commit(self) -> None:
        if self._finished:
            raise UnitOfWorkStateError("unit of work transaction is already finished")
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            self._finished = True
            raise
        self._finished = True

    def rollback(self) -> None:
        if self._session is None or self._finished:
            return
        self._session.rollback()
        self._finished = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        session, self._session = self._session, None
        if session is None:
            return
        try:
            if not self._finished:
                session.rollback()
        finally:
            session.close()
            self._finished = False


__all__ = ["SqlAlchemyUnitOfWork", "UnitOfWorkStateError"]
