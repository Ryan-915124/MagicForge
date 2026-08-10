"""Administrator-only HTTP catalogue for immutable governance audit events."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.security_runtime import (
    AuditReadDependency,
    dependency_failure,
    get_security_services,
    security_http_exception,
)
from security.audit_read import AuditEventSummary
from security.services import SecurityServiceError


router = APIRouter()


class AuditEventResponse(BaseModel):
    event_id: UUID
    event_type: str
    actor_user_id: UUID
    actor_username: str
    actor_role_snapshot: list[str] = Field(default_factory=list)
    object_type: str
    object_id: str
    domain_schema_version: str
    payload_checksum: str | None
    request_id: str | None
    correlation_id: str
    created_at: datetime
    previous_state_fields: list[str] = Field(default_factory=list)
    new_state_fields: list[str] = Field(default_factory=list)
    metadata_fields: list[str] = Field(default_factory=list)

    @classmethod
    def from_summary(cls, event: AuditEventSummary) -> "AuditEventResponse":
        return cls(
            event_id=event.event_id,
            event_type=event.event_type,
            actor_user_id=event.actor_user_id,
            actor_username=event.actor_username,
            actor_role_snapshot=list(event.actor_role_snapshot),
            object_type=event.object_type,
            object_id=event.object_id,
            domain_schema_version=event.domain_schema_version,
            payload_checksum=event.payload_checksum,
            request_id=event.request_id,
            correlation_id=event.correlation_id,
            created_at=event.created_at,
            previous_state_fields=list(event.previous_state_fields),
            new_state_fields=list(event.new_state_fields),
            metadata_fields=list(event.metadata_fields),
        )


class AuditEventsResponse(BaseModel):
    items: list[AuditEventResponse] = Field(default_factory=list)
    limit: int
    next_cursor: str | None
    has_more: bool


@router.get("/audit/events", response_model=AuditEventsResponse)
async def list_audit_events(
    request: Request,
    response: Response,
    actor: AuditReadDependency,
    actor_user_id: UUID | None = Query(default=None),
    event_type: str | None = Query(default=None, min_length=1, max_length=120),
    object_type: str | None = Query(default=None, min_length=1, max_length=120),
    object_id: str | None = Query(default=None, min_length=1, max_length=255),
    request_id: str | None = Query(default=None, min_length=1, max_length=255),
    correlation_id: str | None = Query(default=None, min_length=1, max_length=255),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None, min_length=1, max_length=512),
    limit: int = Query(default=50, ge=1, le=100),
) -> AuditEventsResponse:
    service = get_security_services(request).audit
    try:
        page = await run_in_threadpool(
            service.list_events,
            actor,
            actor_user_id=actor_user_id,
            event_type=event_type,
            object_type=object_type,
            object_id=object_id,
            request_id=request_id,
            correlation_id=correlation_id,
            created_from=created_from,
            created_to=created_to,
            cursor=cursor,
            limit=limit,
        )
    except SecurityServiceError as exc:
        raise security_http_exception(exc) from exc
    except SQLAlchemyError as exc:
        raise dependency_failure() from exc

    response.headers["Cache-Control"] = "no-store"
    return AuditEventsResponse(
        items=[AuditEventResponse.from_summary(event) for event in page.items],
        limit=limit,
        next_cursor=page.next_cursor,
        has_more=page.next_cursor is not None,
    )


__all__ = ["router"]
