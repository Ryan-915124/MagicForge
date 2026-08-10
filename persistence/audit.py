"""Append-only AuditEvent persistence adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from persistence.models import AuditEvent, RoleName


class AuditMetadataError(ValueError):
    """Raised when an audit payload is unsafe or cannot be serialized."""


_SENSITIVE_KEYS = {
    "authorization",
    "authorizationheader",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "password",
    "passwordhash",
    "secret",
    "secretkey",
    "clientsecret",
    "apikey",
    "glmapikey",
    "accesstoken",
    "refreshtoken",
    "sessiontoken",
    "token",
    "tokenhash",
}


class AuditEventRepository:
    """Insert/query-only repository bound to an existing transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        *,
        event_type: str,
        actor_user_id: UUID,
        actor_role_snapshot: Sequence[RoleName | str],
        object_type: str,
        object_id: str,
        reason: str,
        previous_state: Mapping[str, Any] | list[Any] | None = None,
        new_state: Mapping[str, Any] | list[Any] | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        domain_schema_version: str = "audit-event-0.1",
        payload_checksum: str | None = None,
        event_id: UUID | None = None,
    ) -> AuditEvent:
        event_type = _required_text(event_type, "event_type")
        object_type = _required_text(object_type, "object_type")
        object_id = _required_text(object_id, "object_id")
        reason = _required_text(reason, "reason")
        domain_schema_version = _required_text(
            domain_schema_version, "domain_schema_version"
        )
        correlation_id = _required_text(
            correlation_id or request_id or str(uuid4()), "correlation_id"
        )
        request_id = request_id.strip() if request_id else None

        roles = sorted({_normalize_role(role) for role in actor_role_snapshot})
        if not roles:
            raise AuditMetadataError("actor_role_snapshot must not be empty")

        safe_previous = _json_copy(previous_state, "previous_state")
        safe_new = _json_copy(new_state, "new_state")
        safe_metadata = _json_copy(dict(metadata or {}), "metadata")
        assert isinstance(safe_metadata, dict)
        _reject_sensitive_metadata(safe_previous)
        _reject_sensitive_metadata(safe_new)
        _reject_sensitive_metadata(safe_metadata)

        if payload_checksum is None and safe_new is not None:
            payload_checksum = _json_checksum(safe_new)
        if payload_checksum is not None:
            payload_checksum = payload_checksum.strip().casefold()
            if len(payload_checksum) != 64 or any(
                character not in "0123456789abcdef" for character in payload_checksum
            ):
                raise AuditMetadataError("payload_checksum must be a SHA-256 hex digest")

        event = AuditEvent(
            event_id=event_id or uuid4(),
            event_type=event_type,
            actor_user_id=actor_user_id,
            actor_role_snapshot=roles,
            object_type=object_type,
            object_id=object_id,
            domain_schema_version=domain_schema_version,
            payload_checksum=payload_checksum,
            previous_state=safe_previous,
            new_state=safe_new,
            reason=reason,
            request_id=request_id,
            correlation_id=correlation_id,
            event_metadata=safe_metadata,
        )
        self._session.add(event)
        # A failed audit insert must fail inside the caller's transaction so
        # the domain state and its audit record roll back together.
        self._session.flush()
        return event

    def get(self, event_id: UUID) -> AuditEvent | None:
        return self._session.get(AuditEvent, event_id)

    def list(
        self,
        *,
        actor_user_id: UUID | None = None,
        event_type: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        statement: Select[tuple[AuditEvent]] = select(AuditEvent)
        if actor_user_id is not None:
            statement = statement.where(AuditEvent.actor_user_id == actor_user_id)
        if event_type is not None:
            statement = statement.where(AuditEvent.event_type == event_type)
        if object_type is not None:
            statement = statement.where(AuditEvent.object_type == object_type)
        if object_id is not None:
            statement = statement.where(AuditEvent.object_id == object_id)
        statement = statement.order_by(
            AuditEvent.created_at.desc(), AuditEvent.event_id.desc()
        ).limit(limit)
        return list(self._session.scalars(statement))

    def list_page(
        self,
        *,
        actor_user_id: UUID | None = None,
        event_type: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        before_created_at: datetime | None = None,
        before_event_id: UUID | None = None,
        limit: int = 100,
    ) -> tuple[list[AuditEvent], bool]:
        """Return one deterministic newest-first page without offset drift.

        The cursor is a pair because multiple events may share the same
        database timestamp. Both cursor values must be provided together.
        Fetching one extra row proves whether another page exists without an
        expensive or racy full-table count.
        """

        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if (before_created_at is None) != (before_event_id is None):
            raise ValueError("audit cursor fields must be provided together")

        statement: Select[tuple[AuditEvent]] = select(AuditEvent)
        if actor_user_id is not None:
            statement = statement.where(AuditEvent.actor_user_id == actor_user_id)
        if event_type is not None:
            statement = statement.where(AuditEvent.event_type == event_type)
        if object_type is not None:
            statement = statement.where(AuditEvent.object_type == object_type)
        if object_id is not None:
            statement = statement.where(AuditEvent.object_id == object_id)
        if request_id is not None:
            statement = statement.where(AuditEvent.request_id == request_id)
        if correlation_id is not None:
            statement = statement.where(AuditEvent.correlation_id == correlation_id)
        if created_from is not None:
            statement = statement.where(AuditEvent.created_at >= created_from)
        if created_to is not None:
            statement = statement.where(AuditEvent.created_at <= created_to)
        if before_created_at is not None and before_event_id is not None:
            statement = statement.where(
                or_(
                    AuditEvent.created_at < before_created_at,
                    and_(
                        AuditEvent.created_at == before_created_at,
                        AuditEvent.event_id < before_event_id,
                    ),
                )
            )

        statement = statement.order_by(
            AuditEvent.created_at.desc(), AuditEvent.event_id.desc()
        ).limit(limit + 1)
        rows = list(self._session.scalars(statement))
        return rows[:limit], len(rows) > limit


def _required_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise AuditMetadataError(f"{field} must not be empty")
    return normalized


def _normalize_role(value: RoleName | str) -> str:
    if isinstance(value, RoleName):
        return value.value
    try:
        return RoleName(_required_text(value, "role").casefold()).value
    except ValueError as exc:
        raise AuditMetadataError("actor_role_snapshot contains an unknown role") from exc


def _json_copy(value: Any, field: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError) as exc:
        raise AuditMetadataError(f"{field} must contain JSON-safe values") from exc


def _json_checksum(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reject_sensitive_metadata(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = "".join(character for character in key.casefold() if character.isalnum())
            if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith(
                ("password", "passwordhash", "token", "tokenhash", "apikey", "secretkey")
            ):
                raise AuditMetadataError(
                    "audit metadata must not contain credentials or secrets"
                )
            _reject_sensitive_metadata(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_metadata(nested)


__all__ = ["AuditEventRepository", "AuditMetadataError"]
