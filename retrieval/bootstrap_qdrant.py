"""Writer for explicitly unverified bootstrap projections only."""

from __future__ import annotations

from qdrant_client import models

from knowledge.bootstrap import (
    BOOTSTRAP_COLLECTION_NAME,
    BootstrapIngestionReceipt,
    BootstrapStorageManifest,
)
from knowledge.governance import MagicForgeMode
from retrieval.qdrant_service import QdrantService, QdrantServiceError


class BootstrapQdrantWriter:
    def __init__(self, service: QdrantService) -> None:
        if service.mode != MagicForgeMode.BOOTSTRAP:
            raise QdrantServiceError("bootstrap writer requires bootstrap runtime mode")
        if service.collection_name != BOOTSTRAP_COLLECTION_NAME:
            raise QdrantServiceError("bootstrap writer received an unsafe collection")
        self.service = service

    def write_manifest(
        self,
        manifest: BootstrapStorageManifest,
    ) -> BootstrapIngestionReceipt:
        if manifest.collection_name != BOOTSTRAP_COLLECTION_NAME:
            raise QdrantServiceError("bootstrap manifest targets an unsafe collection")
        if manifest.collection_name != self.service.collection_name:
            raise QdrantServiceError("bootstrap manifest targets a different collection")
        self.service.create_collection()
        try:
            vectors = self.service.embedding_provider.embed_documents(
                [projection.text for projection in manifest.projections]
            )
            points = [
                models.PointStruct(
                    id=projection.knowledge_unit_id,
                    vector=vector,
                    payload=projection.to_payload(
                        manifest_id=manifest.id,
                        manifest_hash=manifest.manifest_hash,
                    ),
                )
                for projection, vector in zip(
                    manifest.projections, vectors, strict=True
                )
            ]
            self.service.client.upsert(
                collection_name=BOOTSTRAP_COLLECTION_NAME,
                points=points,
                wait=True,
            )
            records = self.service.client.retrieve(
                collection_name=BOOTSTRAP_COLLECTION_NAME,
                ids=manifest.expected_point_ids,
                with_payload=True,
                with_vectors=False,
            )
        except QdrantServiceError:
            raise
        except Exception as exc:
            raise QdrantServiceError(f"could not write bootstrap manifest: {exc}") from exc

        actual_ids = {str(record.id) for record in records}
        if actual_ids != set(manifest.expected_point_ids):
            raise QdrantServiceError(
                f"bootstrap verification returned {len(actual_ids)} of "
                f"{manifest.expected_point_count} points"
            )
        checksums = {
            projection.knowledge_unit_id: projection.payload_checksum
            for projection in manifest.projections
        }
        for record in records:
            payload = record.payload or {}
            point_id = str(record.id)
            if payload.get("payload_checksum") != checksums[point_id]:
                raise QdrantServiceError(
                    f"bootstrap payload checksum mismatch for {point_id}"
                )
            if not payload.get("bootstrap_generated"):
                raise QdrantServiceError(f"bootstrap marker missing for {point_id}")
            if payload.get("human_verified") is not False:
                raise QdrantServiceError(
                    f"bootstrap point {point_id} incorrectly claims verification"
                )
            if payload.get("review_status") != "bootstrap":
                raise QdrantServiceError(
                    f"bootstrap point {point_id} has an invalid review status"
                )
        return BootstrapIngestionReceipt(
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            point_ids=manifest.expected_point_ids,
        )


__all__ = ["BootstrapQdrantWriter"]
