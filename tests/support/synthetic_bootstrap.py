"""Build a small, self-authored Bootstrap corpus in a temporary directory.

The private ``research/runs`` tree is intentionally absent from the public
repository.  Runtime tests still need a complete, internally consistent v0.3
descriptor, so this module derives one from the committed public Demo corpus.
No external source text or private run metadata is copied into the fixture.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.config import PROJECT_ROOT, Settings
from knowledge.demo import load_demo_bundle
from knowledge.governance import MagicForgeMode


BOOTSTRAP_COLLECTION = "magicforge_bootstrap_v03"
BOOTSTRAP_MANIFEST_SCHEMA = "bootstrap-manifest-0.2"
GENERATED_AT = "2026-01-01T00:00:00Z"
INGESTED_AT = "2026-01-01T00:01:00Z"
TESTED_AT = "2026-01-01T00:02:00Z"


@dataclass(frozen=True, slots=True)
class SyntheticBootstrapCorpus:
    project_root: Path
    root: Path
    manifest_id: str
    manifest_hash: str
    receipt_id: str
    point_count: int
    source_count: int
    evidence_card_count: int
    knowledge_node_count: int
    relationship_count: int
    renderable_relationship_count: int
    source_categories: dict[str, int]
    artifact_types: dict[str, int]
    domain_memberships: dict[str, int]
    knowledge_origins: dict[str, int]


def write_synthetic_bootstrap(project_root: Path) -> SyntheticBootstrapCorpus:
    """Write a deterministic, complete Bootstrap fixture below ``project_root``."""

    project_root = Path(project_root).resolve()
    root = project_root / "research/runs/bootstrap-002"
    bundle = load_demo_bundle(PROJECT_ROOT / "data/demo/corpus.json")
    projections = [item.model_dump(mode="json") for item in bundle.projections]
    projection_pairs = sorted(
        (item.knowledge_unit_id, item.payload_checksum)
        for item in bundle.projections
    )
    manifest_hash = _json_hash(
        {
            "schema_version": BOOTSTRAP_MANIFEST_SCHEMA,
            "collection_name": BOOTSTRAP_COLLECTION,
            "projections": projection_pairs,
        }
    )
    manifest_id = str(
        uuid5(
            NAMESPACE_URL,
            f"magicforge:bootstrap-manifest:{manifest_hash}",
        )
    )
    receipt_id = str(
        uuid5(
            NAMESPACE_URL,
            f"magicforge:bootstrap-receipt:{manifest_id}:{manifest_hash}",
        )
    )
    point_ids = [item.knowledge_unit_id for item in bundle.projections]

    manifest = {
        "id": manifest_id,
        "schema_version": BOOTSTRAP_MANIFEST_SCHEMA,
        "manifest_hash": manifest_hash,
        "collection_name": BOOTSTRAP_COLLECTION,
        "projections": projections,
        "expected_point_ids": point_ids,
        "expected_point_count": len(point_ids),
        "bootstrap_generated": True,
        "human_authorized": False,
        "created_by": "public-synthetic-test-fixture",
        "created_at": GENERATED_AT,
    }
    receipt = {
        "id": receipt_id,
        "manifest_id": manifest_id,
        "manifest_hash": manifest_hash,
        "collection_name": BOOTSTRAP_COLLECTION,
        "point_ids": point_ids,
        "actor": "public-synthetic-test-fixture",
        "ingested_at": INGESTED_AT,
        "bootstrap_generated": True,
        "human_verified": False,
    }

    artifact_types = Counter(
        item.artifact_type.value for item in bundle.projections
    )
    domain_memberships = Counter(
        domain.value for item in bundle.projections for domain in item.domain
    )
    knowledge_origins = Counter(
        item.knowledge_origin.value for item in bundle.projections
    )
    review_statuses = Counter(
        item.review_status.value for item in bundle.projections
    )
    evidence_count = len(bundle.evidence_cards)
    node_count = len(bundle.nodes)
    relationship_count = len(bundle.relationships)
    source_count = len(bundle.sources)
    source_categories = {"academic": 3, "practitioner": source_count - 3}
    counts = {
        "queries_executed": 2,
        "raw_discovery_results": source_count,
        "verified_new_sources": source_count,
        "sources_processed_total": source_count,
        "sources_with_projected_knowledge": source_count,
        "sources_extracted_successfully": source_count,
        "claims_generated": evidence_count,
        "extraction_errors": 0,
        "evidence_cards_generated": evidence_count,
        "evidence_cards_projected": evidence_count,
        "knowledge_nodes_generated": node_count,
        "knowledge_nodes_projected": node_count,
        "semantic_proposals_rejected": 0,
        "relationships_generated": relationship_count,
        "relationships_projected": relationship_count,
        "qdrant_points_created": len(point_ids),
        "sources_requiring_human_review": source_count,
        "contradiction_checks_pending": evidence_count,
    }
    summary = {
        "run_id": "bootstrap-002",
        "generated_at": GENERATED_AT,
        "mode": "bootstrap",
        "collection": BOOTSTRAP_COLLECTION,
        "counts": counts,
        "manifest": {
            "id": manifest_id,
            "hash": manifest_hash,
            "receipt_id": receipt_id,
            "point_count": len(point_ids),
        },
        "safety": {
            "bootstrap_generated_points": len(point_ids),
            "human_verified_points": 0,
            "approved_points": 0,
            "storage_permission_points": 0,
            "safety_excluded_projection_count": 0,
            "production_collection_touched": False,
            "review_status_counts": dict(review_statuses),
        },
        "human_review_queue": {
            "sources": source_count,
            "evidence_cards": evidence_count,
            "knowledge_nodes": node_count,
            "relationships": relationship_count,
            "contradiction_checks_pending": evidence_count,
            "procedural_method_projections_quarantined": 0,
        },
    }
    smoke = {
        "collection": BOOTSTRAP_COLLECTION,
        "collection_count": len(point_ids),
        "tested_at": TESTED_AT,
        "queries": {
            "attention": {"returned": 3},
            "performance": {"returned": 3},
        },
        "all_returned_hits_bootstrap_safe": True,
        "safety_excluded_points_found": 0,
        "production_collection_present": False,
    }
    registry = {
        "nodes": [item.model_dump(mode="json") for item in bundle.nodes],
        "relationships": [
            item.model_dump(mode="json") for item in bundle.relationships
        ],
    }

    _write_json(
        root / "qdrant_manifest/bootstrap-manifest-v03.json", manifest
    )
    _write_json(
        root / "qdrant_manifest/ingestion-receipt-v03.json", receipt
    )
    # The legacy filename exists only to prove that the v0.3 loader never
    # falls back to it when the authoritative descriptor is absent.
    _write_json(root / "qdrant_manifest/bootstrap-manifest.json", {"legacy": True})
    _write_json(root / "reports/run-summary.json", summary)
    _write_json(root / "reports/retrieval-smoke-test.json", smoke)
    _write_json(root / "knowledge_nodes/_canonical_registry.json", registry)
    _write_json(
        root / "evidence_cards/synthetic-evidence.json",
        [item.model_dump(mode="json") for item in bundle.evidence_cards],
    )

    for index, source in enumerate(bundle.sources):
        record = source.model_dump(mode="json")
        record["source_category"] = (
            "academic" if index < source_categories["academic"] else "practitioner"
        )
        record["human_verified"] = False
        _write_json(root / f"sources/source-{index:02d}.json", record)

    storage_root = root / "qdrant_storage_v03"
    _write_json(
        storage_root / "meta.json",
        {
            "collections": {
                BOOTSTRAP_COLLECTION: {
                    "vectors": {"size": 384, "distance": "Cosine"}
                }
            }
        },
    )
    storage_file = storage_root / "collection" / BOOTSTRAP_COLLECTION / "storage.sqlite"
    storage_file.parent.mkdir(parents=True, exist_ok=True)
    storage_file.touch()

    _write_history_report(
        project_root / "research/runs/bootstrap-001/run-report.json",
        run_id="bootstrap-001",
        collection="magicforge_bootstrap_v01",
        point_count=7,
    )
    _write_history_report(
        project_root / "research/runs/bootstrap-001-v02/run-report.json",
        run_id="bootstrap-001-v02",
        collection="magicforge_bootstrap_v02",
        point_count=11,
    )

    return SyntheticBootstrapCorpus(
        project_root=project_root,
        root=root,
        manifest_id=manifest_id,
        manifest_hash=manifest_hash,
        receipt_id=receipt_id,
        point_count=len(point_ids),
        source_count=source_count,
        evidence_card_count=evidence_count,
        knowledge_node_count=node_count,
        relationship_count=relationship_count,
        renderable_relationship_count=relationship_count,
        source_categories=source_categories,
        artifact_types=dict(sorted(artifact_types.items())),
        domain_memberships=dict(sorted(domain_memberships.items())),
        knowledge_origins=dict(sorted(knowledge_origins.items())),
    )


def bootstrap_settings(
    fixture: SyntheticBootstrapCorpus,
    **changes: Any,
) -> Settings:
    """Return settings bound only to the temporary synthetic corpus."""

    values: dict[str, Any] = {
        "glm_api_key": "test-key",
        "magicforge_mode": MagicForgeMode.BOOTSTRAP,
        "bootstrap_allow_anonymous_reads": True,
        "qdrant_bootstrap_collection_name": BOOTSTRAP_COLLECTION,
        "active_corpus_root": str(fixture.root),
        "active_corpus_id": "bootstrap-002",
        "active_corpus_manifest_path": str(
            fixture.root / "qdrant_manifest/bootstrap-manifest-v03.json"
        ),
        "active_corpus_receipt_path": str(
            fixture.root / "qdrant_manifest/ingestion-receipt-v03.json"
        ),
        "active_corpus_manifest_schema": BOOTSTRAP_MANIFEST_SCHEMA,
        "active_qdrant_storage_kind": "local",
        "active_qdrant_local_path": str(fixture.root / "qdrant_storage_v03"),
        "embedding_dimension": 384,
    }
    values.update(changes)
    return Settings(**values)


def _write_history_report(
    path: Path,
    *,
    run_id: str,
    collection: str,
    point_count: int,
) -> None:
    _write_json(
        path,
        {
            "run_id": run_id,
            "generated_at": GENERATED_AT,
            "mode": "bootstrap",
            "sources_processed": 2,
            "sources_extracted_successfully": 2,
            "claims_generated": 3,
            "evidence_cards_generated": 3,
            "knowledge_nodes_generated": 2,
            "relationships_generated": 1,
            "qdrant_points_created": point_count,
            "extraction_errors": 0,
            "qdrant_ingestion_succeeded": True,
            "safety": {"bootstrap_collection": collection},
        },
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _json_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
