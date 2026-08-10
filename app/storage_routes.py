"""Authenticated HTTP boundary for Production storage governance."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.security_runtime import enforce_actor_csrf, get_authenticated_actor, request_context
from research.review.storage_workflow_models import (
    CorpusActivationCommand,
    CorpusVersionView,
    CorpusVersionsView,
    EligibleArtifactView,
    IngestionResultView,
    ManifestAuthorizationCommand,
    ManifestBuildCommand,
    ManifestIngestionCommand,
    StorageManifestSummaryView,
    StorageManifestView,
)
from research.review.storage_workflow_service import (
    ELIGIBLE_ARTIFACT_MAX_OFFSET,
    ProductionStorageWorkflowService,
    StorageWorkflowError,
)
from security.policy import AuthenticatedActor


router = APIRouter()

ActorDependency = Annotated[AuthenticatedActor, Depends(get_authenticated_actor)]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


async def get_storage_workflow_service(
    request: Request,
) -> ProductionStorageWorkflowService:
    service = getattr(request.app.state, "storage_workflow_service", None)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "storage_workflow_not_ready",
                "message": "Production storage governance is unavailable.",
            },
        )
    return service


StorageServiceDependency = Annotated[
    ProductionStorageWorkflowService, Depends(get_storage_workflow_service)
]


class EligibleArtifactsResponse(BaseModel):
    items: list[EligibleArtifactView] = Field(default_factory=list)
    limit: int
    offset: int


class StorageManifestsResponse(BaseModel):
    items: list[StorageManifestSummaryView] = Field(default_factory=list)
    limit: int
    offset: int


@router.post(
    "/storage/manifests",
    response_model=StorageManifestView,
    status_code=status.HTTP_201_CREATED,
)
async def build_manifest(
    payload: ManifestBuildCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: ActorDependency,
    service: StorageServiceDependency,
) -> StorageManifestView:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.build_manifest, actor, payload, idempotency_key=idempotency_key,
        request_id=request_id, correlation_id=correlation_id,
    )


@router.get("/storage/eligible-artifacts", response_model=EligibleArtifactsResponse)
async def list_eligible_artifacts(
    actor: ActorDependency,
    service: StorageServiceDependency,
    artifact_type: Literal[
        "evidence_card", "knowledge_node", "relationship"
    ] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=ELIGIBLE_ARTIFACT_MAX_OFFSET),
) -> EligibleArtifactsResponse:
    items = await _invoke(
        service.list_eligible_artifacts,
        actor,
        artifact_type=artifact_type,
        limit=limit,
        offset=offset,
    )
    return EligibleArtifactsResponse(items=items, limit=limit, offset=offset)


@router.get("/storage/manifests", response_model=StorageManifestsResponse)
async def list_manifests(
    actor: ActorDependency,
    service: StorageServiceDependency,
    manifest_status: Literal["pending", "authorized", "ingested"] | None = Query(
        default=None, alias="status"
    ),
    corpus_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> StorageManifestsResponse:
    items = await _invoke(
        service.list_manifests,
        actor,
        status=manifest_status,
        corpus_id=corpus_id,
        limit=limit,
        offset=offset,
    )
    return StorageManifestsResponse(items=items, limit=limit, offset=offset)


@router.get("/storage/manifests/{manifest_id}", response_model=StorageManifestView)
async def get_manifest(
    manifest_id: UUID,
    actor: ActorDependency,
    service: StorageServiceDependency,
) -> StorageManifestView:
    return await _invoke(service.get_manifest, actor, manifest_id)


@router.post(
    "/storage/manifests/{manifest_id}/authorize",
    response_model=StorageManifestView,
)
async def authorize_manifest(
    manifest_id: UUID,
    payload: ManifestAuthorizationCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: ActorDependency,
    service: StorageServiceDependency,
) -> StorageManifestView:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.authorize_manifest, actor, manifest_id, payload,
        idempotency_key=idempotency_key, request_id=request_id,
        correlation_id=correlation_id,
    )


@router.post(
    "/storage/manifests/{manifest_id}/ingest",
    response_model=IngestionResultView,
)
async def ingest_manifest(
    manifest_id: UUID,
    payload: ManifestIngestionCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: ActorDependency,
    service: StorageServiceDependency,
) -> IngestionResultView:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.ingest_manifest, actor, manifest_id, payload,
        idempotency_key=idempotency_key, request_id=request_id,
        correlation_id=correlation_id,
    )


@router.post("/corpora/{corpus_id}/activate", response_model=CorpusVersionView)
async def activate_corpus(
    corpus_id: UUID,
    payload: CorpusActivationCommand,
    request: Request,
    idempotency_key: IdempotencyKey,
    actor: ActorDependency,
    service: StorageServiceDependency,
) -> CorpusVersionView:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.activate_corpus, actor, corpus_id, payload,
        idempotency_key=idempotency_key, request_id=request_id,
        correlation_id=correlation_id,
    )


@router.get("/corpora/active", response_model=CorpusVersionView)
async def get_active_corpus(
    actor: ActorDependency,
    service: StorageServiceDependency,
    runtime_scope: str = "production",
) -> CorpusVersionView:
    return await _invoke(
        service.get_active_corpus, actor, runtime_scope=runtime_scope
    )


@router.get("/corpora", response_model=CorpusVersionsView)
async def list_corpora(
    actor: ActorDependency,
    service: StorageServiceDependency,
) -> CorpusVersionsView:
    return await _invoke(service.list_corpora, actor)


async def _invoke(call, /, *args, **kwargs):
    try:
        return await run_in_threadpool(call, *args, **kwargs)
    except StorageWorkflowError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.public_message},
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "storage_persistence_unavailable",
                "message": "Production storage persistence is unavailable.",
            },
        ) from exc


__all__ = ["get_storage_workflow_service", "router"]
