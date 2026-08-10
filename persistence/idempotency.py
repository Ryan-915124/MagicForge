"""Transactional idempotency adapter used by governance mutations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from persistence.models import IdempotencyRecord, IdempotencyStatus


class IdempotencyError(ValueError):
    pass


class IdempotencyConflictError(IdempotencyError):
    pass


class IdempotencyInProgressError(IdempotencyError):
    pass


@dataclass(frozen=True, slots=True)
class IdempotencyReplay:
    status_code: int
    body: dict[str, Any] | list[Any]


def canonical_payload_hash(payload: Any) -> str:
    """Return a stable SHA-256 digest without accepting non-JSON values."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class IdempotencyRepository:
    """Bind one idempotency decision to the caller's existing transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def begin(
        self,
        *,
        actor_user_id: UUID,
        scope: str,
        key: str,
        request_hash: str,
        now: datetime,
    ) -> tuple[IdempotencyRecord, IdempotencyReplay | None]:
        normalized_scope = _bounded_required(scope, "scope", 255)
        normalized_key = _bounded_required(key, "idempotency key", 255)
        if len(request_hash) != 64:
            raise IdempotencyError("request_hash must be a SHA-256 digest")
        statement = (
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.actor_user_id == actor_user_id,
                IdempotencyRecord.scope == normalized_scope,
                IdempotencyRecord.idempotency_key == normalized_key,
            )
            .with_for_update()
        )
        record = self._session.scalar(statement)
        if record is not None:
            return self._existing(record, request_hash)

        aware_now = _aware_utc(now)
        record = IdempotencyRecord(
            actor_user_id=actor_user_id,
            scope=normalized_scope,
            idempotency_key=normalized_key,
            request_hash=request_hash,
            status=IdempotencyStatus.IN_PROGRESS,
            locked_until=aware_now + timedelta(minutes=5),
            expires_at=aware_now + timedelta(days=7),
            created_at=aware_now,
            updated_at=aware_now,
        )
        bind = self._session.get_bind()
        if bind.dialect.name != "postgresql":
            # SQLite is used only for isolated tests. Releasing its first
            # SAVEPOINT may commit the write independently of the surrounding
            # unit of work, so preserve strict rollback semantics here.
            self._session.add(record)
            self._session.flush()
        else:
            try:
                # The savepoint keeps the outer governance transaction usable
                # if a concurrent request wins the unique key insert.
                with self._session.begin_nested():
                    self._session.add(record)
                    self._session.flush()
            except IntegrityError:
                winner = self._session.scalar(statement)
                if winner is None:
                    raise IdempotencyInProgressError(
                        "the idempotency key is being acquired concurrently"
                    )
                return self._existing(winner, request_hash)
        return record, None

    @staticmethod
    def _existing(
        record: IdempotencyRecord,
        request_hash: str,
    ) -> tuple[IdempotencyRecord, IdempotencyReplay | None]:
        if record.request_hash != request_hash:
            raise IdempotencyConflictError(
                "the idempotency key was already used with a different payload"
            )
        if record.status == IdempotencyStatus.COMPLETED:
            if record.response_status_code is None or record.response_body is None:
                raise IdempotencyError("completed idempotency record is incomplete")
            body = json.loads(json.dumps(record.response_body, ensure_ascii=False))
            if not isinstance(body, (dict, list)):
                raise IdempotencyError("stored idempotency response is invalid")
            return record, IdempotencyReplay(record.response_status_code, body)
        raise IdempotencyInProgressError(
            "an operation with this idempotency key is already in progress"
        )

    def complete(
        self,
        record: IdempotencyRecord,
        *,
        status_code: int,
        response_body: dict[str, Any] | list[Any],
        now: datetime,
    ) -> None:
        if record.status != IdempotencyStatus.IN_PROGRESS:
            raise IdempotencyError("only in-progress operations may be completed")
        if not 100 <= status_code <= 599:
            raise IdempotencyError("response status code is invalid")
        body = json.loads(
            json.dumps(
                response_body,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                default=_json_default,
            )
        )
        aware_now = _aware_utc(now)
        record.status = IdempotencyStatus.COMPLETED
        record.response_status_code = status_code
        record.response_body = body
        record.error_code = None
        record.locked_until = None
        record.completed_at = aware_now
        record.updated_at = aware_now
        self._session.flush()


def _json_default(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _aware_utc(value).isoformat()
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bounded_required(value: str, label: str, maximum: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise IdempotencyError(f"{label} is required")
    if len(normalized) > maximum:
        raise IdempotencyError(f"{label} is too long")
    return normalized


__all__ = [
    "IdempotencyConflictError",
    "IdempotencyError",
    "IdempotencyInProgressError",
    "IdempotencyReplay",
    "IdempotencyRepository",
    "canonical_payload_hash",
]
