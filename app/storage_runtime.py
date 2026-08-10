"""Runtime composition for Production manifest and corpus governance."""

from __future__ import annotations

from app.config import Settings
from app.database_runtime import DatabaseRuntime, DatabaseRuntimeError
from knowledge.governance import MagicForgeMode
from research.review.storage_workflow_service import ProductionStorageWorkflowService
from research.review.storage_workflow_models import (
    PersistentProductionWriteCapability,
)
from retrieval.embeddings import FastEmbedProvider
from retrieval.qdrant_service import QdrantService


def build_storage_workflow_service(
    database_runtime: DatabaseRuntime,
    settings: Settings,
) -> ProductionStorageWorkflowService | None:
    try:
        session_factory = database_runtime.require_database_ready()
    except DatabaseRuntimeError:
        if database_runtime.required:
            raise
        return None
    if settings.magicforge_mode != MagicForgeMode.PRODUCTION:
        # Bootstrap data and collections remain completely outside this writer.
        return None
    writer = QdrantService(
        url=settings.qdrant_url,
        collection_name=settings.qdrant_collection_name,
        embedding_provider=FastEmbedProvider(
            settings.embedding_model, settings.embedding_dimension
        ),
        mode=MagicForgeMode.PRODUCTION,
        production_writes_enabled=settings.production_qdrant_writes_enabled,
    )
    persistent_capability = None
    if settings.production_qdrant_write_manifest_id:
        persistent_capability = PersistentProductionWriteCapability(
            manifest_id=settings.production_qdrant_write_manifest_id,
            manifest_hash=settings.production_qdrant_write_manifest_hash,
            collection_name=settings.production_qdrant_write_collection,
            point_count=settings.production_qdrant_write_point_count,
        )
    return ProductionStorageWorkflowService(
        session_factory,
        writer=writer,
        allow_temporary_qdrant_writes=False,
        persistent_production_write_approved=(
            settings.production_qdrant_writes_enabled
        ),
        persistent_write_capability=persistent_capability,
        vector_size=settings.embedding_dimension,
        vector_distance="cosine",
    )


__all__ = ["build_storage_workflow_service"]
