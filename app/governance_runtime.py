"""Runtime ownership for the Checkpoint 3 governance application service."""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.database_runtime import DatabaseRuntime, DatabaseRuntimeError
from research.review.workflow_service import ProductionGovernanceService


def build_governance_service(
    database_runtime: DatabaseRuntime,
) -> ProductionGovernanceService | None:
    try:
        session_factory = database_runtime.require_database_ready()
    except DatabaseRuntimeError:
        if database_runtime.required:
            raise
        return None
    return ProductionGovernanceService(session_factory)


async def get_governance_service(request: Request) -> ProductionGovernanceService:
    service: ProductionGovernanceService | None = request.app.state.governance_service
    if service is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "governance_persistence_not_ready",
                "message": "Governance persistence is unavailable.",
            },
        )
    return service


__all__ = ["build_governance_service", "get_governance_service"]
