"""SQLAlchemy repositories for users, roles, and opaque sessions."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from persistence.models import (
    Role,
    RoleName,
    SessionRecord,
    SessionStatus,
    User,
    UserRole,
)


def normalize_identifier(value: str) -> str:
    """Normalize a username/email for deterministic identity lookup."""

    if not isinstance(value, str):
        raise ValueError("identifier must be text")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not normalized:
        raise ValueError("identifier must not be empty")
    return normalized


class UserRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, user: User) -> None:
        self._session.add(user)

    def get(self, user_id: UUID, *, for_update: bool = False) -> User | None:
        statement = select(User).where(User.id == user_id)
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def get_by_identifier(
        self, identifier: str, *, for_update: bool = False
    ) -> User | None:
        normalized = normalize_identifier(identifier)
        statement = select(User).where(
            or_(
                User.username_normalized == normalized,
                User.email_normalized == normalized,
            )
        )
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def count(self) -> int:
        return int(self._session.scalar(select(func.count()).select_from(User)) or 0)

    def usernames_for_ids(self, user_ids: Iterable[UUID]) -> dict[UUID, str]:
        ids = tuple(set(user_ids))
        if not ids:
            return {}
        statement = select(User.id, User.username).where(User.id.in_(ids))
        return {user_id: username for user_id, username in self._session.execute(statement)}


class RoleRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, name: RoleName) -> Role | None:
        return self._session.scalar(select(Role).where(Role.name == name))

    def active_names_for_user(self, user_id: UUID) -> frozenset[RoleName]:
        statement = (
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id, UserRole.revoked_at.is_(None))
        )
        return frozenset(self._session.scalars(statement))

    def grant(
        self,
        *,
        user_id: UUID,
        role: Role,
        granted_by_user_id: UUID,
        granted_at: datetime,
    ) -> tuple[UserRole, bool]:
        assignment = self._session.get(UserRole, (user_id, role.id))
        if assignment is not None and assignment.revoked_at is None:
            return assignment, False
        if assignment is None:
            assignment = UserRole(
                user_id=user_id,
                role_id=role.id,
                granted_by_user_id=granted_by_user_id,
                granted_at=granted_at,
            )
            self._session.add(assignment)
        else:
            assignment.granted_by_user_id = granted_by_user_id
            assignment.granted_at = granted_at
            assignment.revoked_at = None
            assignment.revoked_by_user_id = None
        return assignment, True

    def revoke(
        self,
        *,
        user_id: UUID,
        role: Role,
        revoked_by_user_id: UUID,
        revoked_at: datetime,
    ) -> tuple[UserRole | None, bool]:
        assignment = self._session.get(UserRole, (user_id, role.id))
        if assignment is None or assignment.revoked_at is not None:
            return assignment, False
        assignment.revoked_at = revoked_at
        assignment.revoked_by_user_id = revoked_by_user_id
        return assignment, True


class SessionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: SessionRecord) -> None:
        self._session.add(record)

    def get(self, session_id: UUID, *, for_update: bool = False) -> SessionRecord | None:
        statement = select(SessionRecord).where(SessionRecord.id == session_id)
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def get_by_token_hash(
        self, token_hash: str, *, for_update: bool = False
    ) -> SessionRecord | None:
        statement = select(SessionRecord).where(SessionRecord.token_hash == token_hash)
        if for_update:
            statement = statement.with_for_update()
        return self._session.scalar(statement)

    def active_for_user(self, user_id: UUID) -> list[SessionRecord]:
        statement = select(SessionRecord).where(
            SessionRecord.user_id == user_id,
            SessionRecord.status == SessionStatus.ACTIVE,
        )
        return list(self._session.scalars(statement))

    @staticmethod
    def revoke(
        record: SessionRecord,
        *,
        revoked_at: datetime,
        revoked_by_user_id: UUID,
        reason: str,
    ) -> bool:
        if record.status == SessionStatus.REVOKED:
            return False
        record.status = SessionStatus.REVOKED
        record.revoked_at = revoked_at
        record.revoked_by_user_id = revoked_by_user_id
        record.revocation_reason = reason
        return True


__all__ = [
    "RoleRepository",
    "SessionRepository",
    "UserRepository",
    "normalize_identifier",
]
