"""Authenticated HTTP boundary for Production Mapping governance."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.mapping_runtime import get_mapping_service
from app.security_runtime import (
    MappingReviewDependency,
    MappingSubmitDependency,
    dependency_failure,
    enforce_actor_csrf,
    request_context,
)
from research.review.mapping_models import (
    CanonicalEntitySummary,
    EntityMappingCommand,
    KnowledgeNodeVersionView,
    KnowledgeRelationshipAssertionView,
    MappingProposalDetail,
    MappingProposalKind,
    MappingProposalSummary,
    MappingReviewCommand,
    MappingReviewResult,
    RelationshipMappingCommand,
)
from knowledge.models import EntityType
from research.review.mapping_service import ProductionMappingService
from research.review.workflow_models import WorkflowStatus
from research.review.workflow_service import GovernanceServiceError


router = APIRouter()

MappingServiceDependency = Annotated[
    ProductionMappingService, Depends(get_mapping_service)
]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        description="Caller-generated retry key scoped to actor and operation.",
    ),
]


class MappingQueueResponse(BaseModel):
    items: list[MappingProposalSummary] = Field(default_factory=list)
    limit: int
    offset: int


class CanonicalEntityQueueResponse(BaseModel):
    items: list[CanonicalEntitySummary] = Field(default_factory=list)
    limit: int
    offset: int


@router.post(
    "/mappings/entities",
    response_model=MappingProposalSummary,
    status_code=status.HTTP_201_CREATED,
)
async def submit_entity_mapping(
    payload: EntityMappingCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: MappingSubmitDependency,
    service: MappingServiceDependency,
) -> MappingProposalSummary:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.submit_entity,
        actor,
        payload,
        idempotency_key=idempotency_key,
        request_id=request_id,
        correlation_id=correlation_id,
    )


@router.post(
    "/mappings/relationships",
    response_model=MappingProposalSummary,
    status_code=status.HTTP_201_CREATED,
)
async def submit_relationship_mapping(
    payload: RelationshipMappingCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: MappingSubmitDependency,
    service: MappingServiceDependency,
) -> MappingProposalSummary:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.submit_relationship,
        actor,
        payload,
        idempotency_key=idempotency_key,
        request_id=request_id,
        correlation_id=correlation_id,
    )


@router.get("/review/mappings", response_model=MappingQueueResponse)
async def list_mapping_queue(
    actor: MappingReviewDependency,
    service: MappingServiceDependency,
    review_status: WorkflowStatus | None = Query(default=None, alias="status"),
    kind: MappingProposalKind | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> MappingQueueResponse:
    items = await _invoke(
        service.list_mappings,
        actor,
        status=review_status,
        kind=kind,
        limit=limit,
        offset=offset,
    )
    return MappingQueueResponse(items=items, limit=limit, offset=offset)


@router.get("/mappings/{mapping_id}", response_model=MappingProposalDetail)
async def get_mapping(
    mapping_id: UUID,
    actor: MappingReviewDependency,
    service: MappingServiceDependency,
) -> MappingProposalDetail:
    return await _invoke(service.get_mapping, actor, mapping_id)


@router.post("/mappings/{mapping_id}/review", response_model=MappingReviewResult)
async def review_mapping(
    mapping_id: UUID,
    payload: MappingReviewCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: MappingReviewDependency,
    service: MappingServiceDependency,
) -> MappingReviewResult:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.review_mapping,
        actor,
        mapping_id,
        payload,
        idempotency_key=idempotency_key,
        request_id=request_id,
        correlation_id=correlation_id,
    )


@router.get(
    "/knowledge/entities", response_model=CanonicalEntityQueueResponse
)
async def list_canonical_entities(
    actor: MappingReviewDependency,
    service: MappingServiceDependency,
    query: str | None = Query(default=None, max_length=200),
    entity_type: EntityType | None = Query(default=None),
    entity_status: Literal["active"] = Query(default="active", alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> CanonicalEntityQueueResponse:
    del entity_status
    items = await _invoke(
        service.list_canonical_entities,
        actor,
        query=query,
        entity_type=entity_type,
        limit=limit,
        offset=offset,
    )
    return CanonicalEntityQueueResponse(items=items, limit=limit, offset=offset)


@router.get(
    "/knowledge/versions/{version_id}", response_model=KnowledgeNodeVersionView
)
async def get_knowledge_node_version(
    version_id: UUID,
    actor: MappingReviewDependency,
    service: MappingServiceDependency,
) -> KnowledgeNodeVersionView:
    return await _invoke(service.get_node_version, actor, version_id)


@router.get(
    "/knowledge/relationship-assertions/{assertion_id}",
    response_model=KnowledgeRelationshipAssertionView,
)
async def get_relationship_assertion(
    assertion_id: UUID,
    actor: MappingReviewDependency,
    service: MappingServiceDependency,
) -> KnowledgeRelationshipAssertionView:
    return await _invoke(service.get_relationship_assertion, actor, assertion_id)


async def _invoke(operation, *args, **kwargs):
    try:
        return await run_in_threadpool(operation, *args, **kwargs)
    except GovernanceServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.public_message},
        ) from exc
    except SQLAlchemyError as exc:
        raise dependency_failure() from exc


__all__ = ["router"]
