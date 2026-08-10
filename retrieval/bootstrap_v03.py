"""Run-specific writer for the isolated ``magicforge_bootstrap_v03`` corpus.

This adapter deliberately reuses the frozen v0.2 projection model byte-for-byte.
Only the operational manifest and collection target are versioned for Run 002.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from qdrant_client import QdrantClient, models

from app.config import get_settings
from knowledge.bootstrap import BootstrapQdrantProjection
from knowledge.governance import MagicForgeMode
from retrieval.embeddings import FastEmbedProvider
from retrieval.qdrant_service import QdrantService


COLLECTION_NAME = "magicforge_bootstrap_v03"
MANIFEST_SCHEMA_VERSION = "bootstrap-manifest-0.2"


class BootstrapV03Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    schema_version: str = MANIFEST_SCHEMA_VERSION
    manifest_hash: str = ""
    collection_name: str = COLLECTION_NAME
    projections: list[BootstrapQdrantProjection] = Field(min_length=1)
    expected_point_ids: list[str] = Field(default_factory=list)
    expected_point_count: int = Field(default=0, ge=0)
    bootstrap_generated: bool = True
    human_authorized: bool = False
    created_by: str = "bootstrap-pipeline-v03-adapter"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def assign_identity(self) -> "BootstrapV03Manifest":
        if self.collection_name != COLLECTION_NAME:
            raise ValueError("Run 002 manifest must target magicforge_bootstrap_v03")
        if not self.bootstrap_generated or self.human_authorized:
            raise ValueError("Run 002 manifest cannot claim human authorization")
        point_ids = [item.knowledge_unit_id for item in self.projections]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("manifest contains duplicate point IDs")
        digest = _json_hash(
            {
                "schema_version": self.schema_version,
                "collection_name": self.collection_name,
                "projections": sorted(
                    (item.knowledge_unit_id, item.payload_checksum)
                    for item in self.projections
                ),
            }
        )
        manifest_id = str(uuid5(NAMESPACE_URL, f"magicforge:bootstrap-manifest:{digest}"))
        if self.id and self.id != manifest_id:
            raise ValueError("manifest ID does not match contents")
        if self.manifest_hash and self.manifest_hash != digest:
            raise ValueError("manifest hash does not match contents")
        if self.expected_point_ids and self.expected_point_ids != point_ids:
            raise ValueError("expected point IDs do not match projections")
        if self.expected_point_count and self.expected_point_count != len(point_ids):
            raise ValueError("expected point count does not match projections")
        object.__setattr__(self, "id", manifest_id)
        object.__setattr__(self, "manifest_hash", digest)
        object.__setattr__(self, "expected_point_ids", point_ids)
        object.__setattr__(self, "expected_point_count", len(point_ids))
        return self


class BootstrapV03Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    manifest_id: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_name: str = COLLECTION_NAME
    point_ids: list[str] = Field(min_length=1)
    actor: str = "bootstrap-pipeline-v03-adapter"
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    bootstrap_generated: bool = True
    human_verified: bool = False

    @model_validator(mode="after")
    def assign_identity(self) -> "BootstrapV03Receipt":
        UUID(self.manifest_id)
        if self.collection_name != COLLECTION_NAME:
            raise ValueError("receipt targets an unsafe collection")
        if not self.bootstrap_generated or self.human_verified:
            raise ValueError("receipt contains unsafe verification flags")
        expected = str(
            uuid5(
                NAMESPACE_URL,
                f"magicforge:bootstrap-receipt:{self.manifest_id}:{self.manifest_hash}",
            )
        )
        if self.id and self.id != expected:
            raise ValueError("receipt ID does not match manifest")
        object.__setattr__(self, "id", expected)
        return self


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--projection-manifest",
        type=Path,
        default=Path("research/runs/bootstrap-002/qdrant_manifest/bootstrap-manifest.json"),
    )
    parser.add_argument(
        "--output", type=Path, default=Path("research/runs/bootstrap-002/qdrant_manifest")
    )
    parser.add_argument(
        "--storage-path",
        type=Path,
        default=Path("research/runs/bootstrap-002/qdrant_storage_v03"),
    )
    parser.add_argument(
        "--safety-exclusions",
        type=Path,
        default=Path("research/runs/bootstrap-002/reports/safety-exclusions.json"),
    )
    args = parser.parse_args()
    receipt = write_v03(
        projection_manifest=args.projection_manifest,
        output=args.output,
        storage_path=args.storage_path,
        safety_exclusions=args.safety_exclusions,
    )
    print(json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False, indent=2))


def write_v03(
    *,
    projection_manifest: Path,
    output: Path,
    storage_path: Path,
    safety_exclusions: Path | None = None,
):
    settings = get_settings()
    if settings.magicforge_mode != MagicForgeMode.BOOTSTRAP:
        raise RuntimeError("v03 bootstrap writer requires MAGICFORGE_MODE=bootstrap")
    raw = json.loads(projection_manifest.read_text(encoding="utf-8"))
    projections = TypeAdapter(list[BootstrapQdrantProjection]).validate_python(
        raw["projections"]
    )
    excluded_source_ids = _excluded_source_ids(safety_exclusions)
    projections = [
        item
        for item in projections
        if item.source_candidate_id not in excluded_source_ids
        and _storage_safe(item)
    ]
    if any(
        not item.bootstrap_generated
        or item.human_verified
        or item.approved
        or item.storage_permission
        for item in projections
    ):
        raise RuntimeError("projection safety markers do not satisfy bootstrap contract")
    manifest = BootstrapV03Manifest(projections=projections)
    output.mkdir(parents=True, exist_ok=True)
    _write(output / "bootstrap-manifest-v03.json", manifest.model_dump(mode="json"))

    embedding = FastEmbedProvider(settings.embedding_model, settings.embedding_dimension)
    client = QdrantClient(path=str(storage_path))
    if client.collection_exists(COLLECTION_NAME):
        collection = client.get_collection(COLLECTION_NAME)
        vectors = collection.config.params.vectors
        if getattr(vectors, "size", None) != embedding.dimension:
            raise RuntimeError("existing v03 collection has the wrong vector dimension")
    else:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=models.VectorParams(
                size=embedding.dimension,
                distance=models.Distance.COSINE,
            ),
        )
    _ensure_indexes(client)
    vectors = embedding.embed_documents([item.text for item in projections])
    points = [
        models.PointStruct(
            id=item.knowledge_unit_id,
            vector=vector,
            payload=item.to_payload(
                manifest_id=manifest.id,
                manifest_hash=manifest.manifest_hash,
            ),
        )
        for item, vector in zip(projections, vectors, strict=True)
    ]
    for start in range(0, len(points), 256):
        client.upsert(
            collection_name=COLLECTION_NAME,
            points=points[start : start + 256],
            wait=True,
        )
    _remove_stale_points(client, set(manifest.expected_point_ids))
    _verify(client, manifest)
    receipt = BootstrapV03Receipt(
        manifest_id=manifest.id,
        manifest_hash=manifest.manifest_hash,
        point_ids=manifest.expected_point_ids,
    )
    _write(output / "ingestion-receipt-v03.json", receipt.model_dump(mode="json"))
    return receipt


def _excluded_source_ids(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    values = {str(item) for item in raw.get("source_candidate_ids", [])}
    for value in values:
        UUID(value)
    return values


def _storage_safe(projection: BootstrapQdrantProjection) -> bool:
    """Fail closed for procedural magic methods in an unverified collection.

    The frozen v0.2 projection still labels all Run 002 material with the broad
    ``general_principle`` exposure level.  Until a human performs secret-level
    review, controlled/restricted method-role projections are not searchable.
    The full audit artifacts remain outside Qdrant for later review.
    """

    return not (
        "method" in {role.value for role in projection.claim_roles}
        and projection.sensitive_information_level.value in {"controlled", "restricted"}
    )


def _remove_stale_points(client: QdrantClient, expected: set[str]) -> None:
    current: set[str] = set()
    offset = None
    while True:
        records, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=256,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        current.update(str(item.id) for item in records)
        if offset is None:
            break
    stale = sorted(current - expected)
    for start in range(0, len(stale), 256):
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=models.PointIdsList(points=stale[start : start + 256]),
            wait=True,
        )


def _ensure_indexes(client: QdrantClient) -> None:
    collection = client.get_collection(COLLECTION_NAME)
    existing = set(getattr(collection, "payload_schema", {}) or {})
    for field, schema in QdrantService._PAYLOAD_INDEXES.items():
        if field not in existing:
            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name=field,
                field_schema=schema,
                wait=True,
            )


def _verify(client: QdrantClient, manifest: BootstrapV03Manifest) -> None:
    records = []
    for start in range(0, manifest.expected_point_count, 256):
        records.extend(
            client.retrieve(
                collection_name=COLLECTION_NAME,
                ids=manifest.expected_point_ids[start : start + 256],
                with_payload=True,
                with_vectors=False,
            )
        )
    if {str(item.id) for item in records} != set(manifest.expected_point_ids):
        raise RuntimeError("v03 Qdrant verification did not return every point")
    if client.count(collection_name=COLLECTION_NAME, exact=True).count != manifest.expected_point_count:
        raise RuntimeError("v03 collection contains points outside the active manifest")
    checksums = {
        item.knowledge_unit_id: item.payload_checksum for item in manifest.projections
    }
    for record in records:
        payload = record.payload or {}
        point_id = str(record.id)
        if payload.get("payload_checksum") != checksums[point_id]:
            raise RuntimeError(f"payload checksum mismatch for {point_id}")
        if payload.get("bootstrap_generated") is not True:
            raise RuntimeError(f"bootstrap marker missing for {point_id}")
        if payload.get("human_verified") is not False:
            raise RuntimeError(f"unsafe human verification marker for {point_id}")
        if payload.get("review_status") != "bootstrap":
            raise RuntimeError(f"unsafe review status for {point_id}")


def _json_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
