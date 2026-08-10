"""Operator HTTP boundary for governed Production extraction jobs."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.extraction_runtime import get_extraction_service
from app.security_runtime import (
    ExtractionRunDependency,
    enforce_actor_csrf,
    request_context,
)
from persistence.models import ExtractionJobStatus
from research.extraction.production_jobs import (
    ExtractionJobCreateCommand,
    ExtractionJobCreateResult,
    ExtractionJobRunResult,
    ExtractionJobView,
    ProductionExtractionService,
    ProductionExtractionServiceError,
)
from research.extraction.production_source import EligibleSourceView


router = APIRouter(prefix="/research/extractions", tags=["research-extractions"])
ExtractionServiceDependency = Annotated[
    ProductionExtractionService, Depends(get_extraction_service)
]


class EligibleSourcesResponse(BaseModel):
    items: list[EligibleSourceView] = Field(default_factory=list)
    limit: int
    offset: int


class ExtractionJobsResponse(BaseModel):
    items: list[ExtractionJobView] = Field(default_factory=list)
    limit: int
    offset: int


@router.get("/eligible", response_model=EligibleSourcesResponse)
async def list_eligible_sources(
    actor: ExtractionRunDependency,
    service: ExtractionServiceDependency,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> EligibleSourcesResponse:
    items = await _invoke(
        service.list_eligible_sources,
        actor,
        limit=limit,
        offset=offset,
    )
    return EligibleSourcesResponse(items=items, limit=limit, offset=offset)


@router.post(
    "",
    response_model=ExtractionJobCreateResult,
    status_code=status.HTTP_201_CREATED,
)
async def queue_extraction(
    payload: ExtractionJobCreateCommand,
    request: Request,
    actor: ExtractionRunDependency,
    service: ExtractionServiceDependency,
) -> ExtractionJobCreateResult:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.queue,
        actor,
        payload,
        request_id=request_id,
        correlation_id=correlation_id,
    )


@router.get("", response_model=ExtractionJobsResponse)
async def list_extractions(
    actor: ExtractionRunDependency,
    service: ExtractionServiceDependency,
    job_status: ExtractionJobStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ExtractionJobsResponse:
    items = await _invoke(
        service.list_jobs,
        actor,
        status=job_status,
        limit=limit,
        offset=offset,
    )
    return ExtractionJobsResponse(items=items, limit=limit, offset=offset)


@router.get("/{job_id}", response_model=ExtractionJobView)
async def get_extraction(
    job_id: UUID,
    actor: ExtractionRunDependency,
    service: ExtractionServiceDependency,
) -> ExtractionJobView:
    return await _invoke(service.get_job, actor, job_id)


@router.post("/{job_id}/run", response_model=ExtractionJobRunResult)
async def run_extraction(
    job_id: UUID,
    request: Request,
    actor: ExtractionRunDependency,
    service: ExtractionServiceDependency,
) -> ExtractionJobRunResult:
    await run_in_threadpool(enforce_actor_csrf, request, actor)
    request_id, correlation_id = request_context(request)
    return await _invoke(
        service.run,
        actor,
        job_id,
        request_id=request_id,
        correlation_id=correlation_id,
    )


async def _invoke(operation, *args, **kwargs):
    try:
        return await run_in_threadpool(operation, *args, **kwargs)
    except ProductionExtractionServiceError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.public_message},
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "extraction_persistence_unavailable",
                "message": "Production extraction persistence is temporarily unavailable.",
            },
        ) from exc


__all__ = ["router"]
