"""Authenticated HTTP boundary for Source and Claim governance."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.governance_runtime import get_governance_service
from app.security_runtime import (
    ClaimReviewDependency,
    ClaimSubmitDependency,
    ProductActorDependency,
    SourceReviewDependency,
    SourceSubmitDependency,
    dependency_failure,
    enforce_actor_csrf,
    request_context,
)
from research.review.workflow_models import (
    ClaimCandidateCommand,
    ClaimCandidateReviewView,
    ClaimCandidateSummary,
    ClaimReviewCommand,
    ClaimReviewResult,
    EvidenceVersionView,
    SourceReviewCommand,
    SourceReviewDetail,
    SourceReviewQueueItem,
    SourceSummary,
    SourcePermissionRequestCommand,
    SourcePermissionRequestView,
    SourceVersionCommand,
    WorkflowStatus,
)
from research.review.workflow_service import (
    GovernanceServiceError,
    ProductionGovernanceService,
)


router = APIRouter()

GovernanceDependency = Annotated[
    ProductionGovernanceService, Depends(get_governance_service)
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


class ClaimReviewQueueResponse(BaseModel):
    items: list[ClaimCandidateSummary] = Field(default_factory=list)
    limit: int
    offset: int


class SourceReviewQueueResponse(BaseModel):
    items: list[SourceReviewQueueItem] = Field(default_factory=list)
    limit: int
    offset: int


class EvidenceVersionsResponse(BaseModel):
    items: list[EvidenceVersionView] = Field(default_factory=list)


@router.post(
    "/sources",
    response_model=SourceSummary,
    status_code=status.HTTP_201_CREATED,
)
async def register_source(
    payload: SourceVersionCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: SourceSubmitDependency,
    service: GovernanceDependency,
) -> SourceSummary:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.register_source,
        actor,
        payload,
        idempotency_key=idempotency_key,
        request_id=request_id,
        correlation_id=correlation_id,
    )


@router.post(
    "/sources/{source_id}/versions",
    response_model=SourceSummary,
    status_code=status.HTTP_201_CREATED,
)
async def create_source_version(
    source_id: UUID,
    payload: SourceVersionCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: SourceSubmitDependency,
    service: GovernanceDependency,
) -> SourceSummary:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.add_source_version,
        actor,
        source_id,
        payload,
        idempotency_key=idempotency_key,
        request_id=request_id,
        correlation_id=correlation_id,
    )


@router.post(
    "/sources/{source_id}/versions/{version_id}/permission-requests",
    response_model=SourcePermissionRequestView,
    status_code=status.HTTP_201_CREATED,
)
async def create_source_permission_request(
    source_id: UUID,
    version_id: UUID,
    payload: SourcePermissionRequestCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: SourceSubmitDependency,
    service: GovernanceDependency,
) -> SourcePermissionRequestView:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.add_source_permission_request,
        actor,
        source_id,
        version_id,
        payload,
        idempotency_key=idempotency_key,
        request_id=request_id,
        correlation_id=correlation_id,
    )


@router.get("/sources/{source_id}", response_model=SourceReviewDetail)
async def get_source(
    source_id: UUID,
    actor: SourceReviewDependency,
    service: GovernanceDependency,
) -> SourceReviewDetail:
    return await _invoke(service.get_source, actor, source_id)


@router.get("/review/sources", response_model=SourceReviewQueueResponse)
async def list_sources(
    actor: SourceReviewDependency,
    service: GovernanceDependency,
    review_status: WorkflowStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> SourceReviewQueueResponse:
    items = await _invoke(
        service.list_sources,
        actor,
        status=review_status,
        limit=limit,
        offset=offset,
    )
    return SourceReviewQueueResponse(items=items, limit=limit, offset=offset)


@router.post("/sources/{source_id}/review", response_model=SourceSummary)
async def review_source(
    source_id: UUID,
    payload: SourceReviewCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: SourceReviewDependency,
    service: GovernanceDependency,
) -> SourceSummary:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.review_source,
        actor,
        source_id,
        payload,
        idempotency_key=idempotency_key,
        request_id=request_id,
        correlation_id=correlation_id,
    )


@router.post(
    "/claims",
    response_model=ClaimCandidateSummary,
    status_code=status.HTTP_201_CREATED,
)
async def submit_claim(
    payload: ClaimCandidateCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: ClaimSubmitDependency,
    service: GovernanceDependency,
) -> ClaimCandidateSummary:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.submit_claim,
        actor,
        payload,
        idempotency_key=idempotency_key,
        request_id=request_id,
        correlation_id=correlation_id,
    )


@router.get("/review/claims", response_model=ClaimReviewQueueResponse)
async def list_claims(
    actor: ClaimReviewDependency,
    service: GovernanceDependency,
    review_status: WorkflowStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ClaimReviewQueueResponse:
    items = await _invoke(
        service.list_claims,
        actor,
        status=review_status,
        limit=limit,
        offset=offset,
    )
    return ClaimReviewQueueResponse(items=items, limit=limit, offset=offset)


@router.get("/claims/{claim_id}", response_model=ClaimCandidateReviewView)
async def get_claim(
    claim_id: UUID,
    actor: ClaimReviewDependency,
    service: GovernanceDependency,
) -> ClaimCandidateReviewView:
    return await _invoke(service.get_claim, actor, claim_id)


@router.post("/claims/{claim_id}/review", response_model=ClaimReviewResult)
async def review_claim(
    claim_id: UUID,
    payload: ClaimReviewCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: ClaimReviewDependency,
    service: GovernanceDependency,
) -> ClaimReviewResult:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.review_claim,
        actor,
        claim_id,
        payload,
        idempotency_key=idempotency_key,
        request_id=request_id,
        correlation_id=correlation_id,
    )


@router.get(
    "/evidence/{identifier}/versions",
    response_model=EvidenceVersionsResponse,
)
async def evidence_versions(
    identifier: UUID,
    actor: ProductActorDependency,
    service: GovernanceDependency,
) -> EvidenceVersionsResponse:
    items = await _invoke(service.list_evidence_versions, actor, identifier)
    return EvidenceVersionsResponse(items=items)


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
