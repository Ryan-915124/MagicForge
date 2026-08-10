"""Runtime ownership for governed Production extraction jobs."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.config import Settings
from app.database_runtime import DatabaseRuntime, DatabaseRuntimeError
from research.extraction.production_jobs import ProductionExtractionService


def build_extraction_service(
    database_runtime: DatabaseRuntime,
    settings: Settings,
) -> ProductionExtractionService | None:
    try:
        session_factory = database_runtime.require_database_ready()
    except DatabaseRuntimeError:
        if database_runtime.required:
            raise
        return None
    return ProductionExtractionService(session_factory, settings)


async def get_extraction_service(request: Request) -> ProductionExtractionService:
    service: ProductionExtractionService | None = request.app.state.extraction_service
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "extraction_persistence_not_ready",
                "message": "Production extraction persistence is unavailable.",
            },
        )
    return service


__all__ = ["build_extraction_service", "get_extraction_service"]
