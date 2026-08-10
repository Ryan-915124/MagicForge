"""Runtime ownership for the Production Mapping governance service."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.database_runtime import DatabaseRuntime, DatabaseRuntimeError
from research.review.mapping_service import ProductionMappingService


def build_mapping_service(
    database_runtime: DatabaseRuntime,
) -> ProductionMappingService | None:
    try:
        session_factory = database_runtime.require_database_ready()
    except DatabaseRuntimeError:
        if database_runtime.required:
            raise
        return None
    return ProductionMappingService(session_factory)


async def get_mapping_service(request: Request) -> ProductionMappingService:
    service: ProductionMappingService | None = request.app.state.mapping_service
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "mapping_persistence_not_ready",
                "message": "Mapping governance persistence is unavailable.",
            },
        )
    return service


__all__ = ["build_mapping_service", "get_mapping_service"]
