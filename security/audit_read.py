"""Read-only, actor-bound access to the immutable governance audit catalogue."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from persistence import (
    AuditEvent,
    AuditEventRepository,
    SessionFactory,
    SessionStatus,
    SqlAlchemyUnitOfWork,
    UserStatus,
)
from security.policy import AuthenticatedActor, Permission, has_permission
from security.repositories import RoleRepository, SessionRepository, UserRepository
from security.services import AuthorizationDeniedError, SecurityInputError


@dataclass(frozen=True, slots=True)
class AuditEventSummary:
    """Credential-safe audit catalogue row.

    State and metadata values deliberately stay inside persistence. The list
    surface exposes only their field names so an administrator can locate an
    event without turning the catalogue into a bulk data-export endpoint.
    """

    event_id: UUID
    event_type: str
    actor_user_id: UUID
    actor_username: str
    actor_role_snapshot: tuple[str, ...]
    object_type: str
    object_id: str
    domain_schema_version: str
    payload_checksum: str | None
    request_id: str | None
    correlation_id: str
    created_at: datetime
    previous_state_fields: tuple[str, ...]
    new_state_fields: tuple[str, ...]
    metadata_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AuditEventPage:
    items: tuple[AuditEventSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class _AuditCursor:
    created_at: datetime
    event_id: UUID
    query_hash: str


class AuditReadService:
    """Query immutable audit events after a live database RBAC check."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def list_events(
        self,
        actor: AuthenticatedActor,
        *,
        actor_user_id: UUID | None = None,
        event_type: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> AuditEventPage:
        if not 1 <= limit <= 100:
            raise SecurityInputError(
                "invalid_audit_limit", "The audit page limit is invalid."
            )

        event_type = _optional_filter(event_type, field="event_type", max_length=120)
        object_type = _optional_filter(object_type, field="object_type", max_length=120)
        object_id = _optional_filter(object_id, field="object_id", max_length=255)
        request_id = _optional_filter(request_id, field="request_id", max_length=255)
        correlation_id = _optional_filter(
            correlation_id, field="correlation_id", max_length=255
        )
        created_from = _optional_aware_utc(created_from, field="created_from")
        created_to = _optional_aware_utc(created_to, field="created_to")
        if created_from is not None and created_to is not None and created_from > created_to:
            raise SecurityInputError(
                "invalid_audit_time_range",
                "The audit time range is invalid.",
            )

        query_hash = _query_hash(
            actor_user_id=actor_user_id,
            event_type=event_type,
            object_type=object_type,
            object_id=object_id,
            request_id=request_id,
            correlation_id=correlation_id,
            created_from=created_from,
            created_to=created_to,
        )
        decoded_cursor = _decode_cursor(cursor) if cursor is not None else None
        if decoded_cursor is not None and decoded_cursor.query_hash != query_hash:
            raise SecurityInputError(
                "audit_cursor_filter_mismatch",
                "The audit pagination cursor does not match the current filters.",
            )

        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            _require_live_audit_permission(unit, actor, now=self._clock())
            rows, has_more = AuditEventRepository(unit.session).list_page(
                actor_user_id=actor_user_id,
                event_type=event_type,
                object_type=object_type,
                object_id=object_id,
                request_id=request_id,
                correlation_id=correlation_id,
                created_from=created_from,
                created_to=created_to,
                before_created_at=(
                    decoded_cursor.created_at if decoded_cursor is not None else None
                ),
                before_event_id=(
                    decoded_cursor.event_id if decoded_cursor is not None else None
                ),
                limit=limit,
            )
            usernames = UserRepository(unit.session).usernames_for_ids(
                event.actor_user_id for event in rows
            )
            items = tuple(
                _summary(event, actor_username=usernames.get(event.actor_user_id, ""))
                for event in rows
            )
            next_cursor = (
                _encode_cursor(rows[-1], query_hash=query_hash)
                if has_more and rows
                else None
            )
        return AuditEventPage(items=items, next_cursor=next_cursor)


def _require_live_audit_permission(
    unit: SqlAlchemyUnitOfWork,
    actor: AuthenticatedActor,
    *,
    now: datetime,
) -> None:
    if (
        actor.user_id is None
        or actor.session_id is None
        or actor.session_transport is None
        or actor.is_bootstrap_anonymous
    ):
        raise AuthorizationDeniedError()

    session_record = SessionRepository(unit.session).get(actor.session_id)
    user = UserRepository(unit.session).get(actor.user_id)
    now = _aware_utc(now)
    if (
        session_record is None
        or session_record.user_id != actor.user_id
        or session_record.status != SessionStatus.ACTIVE
        or _aware_utc(session_record.expires_at) <= now
        or session_record.transport != actor.session_transport
        or user is None
        or user.status != UserStatus.ACTIVE
    ):
        raise AuthorizationDeniedError()

    roles = RoleRepository(unit.session).active_names_for_user(actor.user_id)
    current_actor = AuthenticatedActor(
        user_id=user.id,
        username=user.username,
        roles=roles,
        session_id=session_record.id,
        session_transport=session_record.transport,
        session_expires_at=_aware_utc(session_record.expires_at),
        break_glass=actor.break_glass,
    )
    if not has_permission(current_actor, Permission.AUDIT_READ):
        raise AuthorizationDeniedError("audit_read_required")


def _optional_filter(
    value: str | None,
    *,
    field: str,
    max_length: int,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SecurityInputError(
            "invalid_audit_filter", f"The {field} audit filter is invalid."
        )
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise SecurityInputError(
            "invalid_audit_filter", f"The {field} audit filter is too long."
        )
    return normalized


def _optional_aware_utc(value: datetime | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise SecurityInputError(
            "invalid_audit_time_range",
            f"The {field} audit filter must include a timezone.",
        )
    return value.astimezone(UTC)


def _query_hash(
    *,
    actor_user_id: UUID | None,
    event_type: str | None,
    object_type: str | None,
    object_id: str | None,
    request_id: str | None,
    correlation_id: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
) -> str:
    encoded = json.dumps(
        {
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "event_type": event_type,
            "object_type": object_type,
            "object_id": object_id,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "created_from": created_from.isoformat() if created_from else None,
            "created_to": created_to.isoformat() if created_to else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _encode_cursor(event: AuditEvent, *, query_hash: str) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "created_at": _aware_utc(event.created_at).isoformat(),
            "event_id": str(event.event_id),
            "query_hash": query_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> _AuditCursor:
    if not isinstance(value, str):
        raise _invalid_cursor()
    encoded = value.strip()
    if not encoded or len(encoded) > 512:
        raise _invalid_cursor()
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "v",
            "created_at",
            "event_id",
            "query_hash",
        }:
            raise ValueError
        if payload["v"] != 1:
            raise ValueError
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None:
            raise ValueError
        event_id = UUID(payload["event_id"])
        query_hash = payload["query_hash"]
        if (
            not isinstance(query_hash, str)
            or len(query_hash) != 64
            or any(character not in "0123456789abcdef" for character in query_hash)
        ):
            raise ValueError
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise _invalid_cursor() from None
    return _AuditCursor(
        created_at=_aware_utc(created_at),
        event_id=event_id,
        query_hash=query_hash,
    )


def _invalid_cursor() -> SecurityInputError:
    return SecurityInputError(
        "invalid_audit_cursor", "The audit pagination cursor is invalid."
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _state_fields(value: Any) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        return tuple(sorted(str(key) for key in value))
    if isinstance(value, list):
        return ("$list",)
    return ()


def _summary(event: AuditEvent, *, actor_username: str) -> AuditEventSummary:
    return AuditEventSummary(
        event_id=event.event_id,
        event_type=event.event_type,
        actor_user_id=event.actor_user_id,
        actor_username=actor_username,
        actor_role_snapshot=tuple(event.actor_role_snapshot),
        object_type=event.object_type,
        object_id=event.object_id,
        domain_schema_version=event.domain_schema_version,
        payload_checksum=event.payload_checksum,
        request_id=event.request_id,
        correlation_id=event.correlation_id,
        created_at=_aware_utc(event.created_at),
        previous_state_fields=_state_fields(event.previous_state),
        new_state_fields=_state_fields(event.new_state),
        metadata_fields=_state_fields(event.event_metadata),
    )


__all__ = ["AuditEventPage", "AuditEventSummary", "AuditReadService"]
