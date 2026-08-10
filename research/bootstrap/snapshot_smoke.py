"""Read-only integrity and retrieval smoke test for an isolated v03 snapshot."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from qdrant_client import QdrantClient

from app.config import get_settings
from research.bootstrap.v03_smoke import QUERIES
from retrieval.bootstrap_v03 import BootstrapV03Manifest, COLLECTION_NAME
from retrieval.embeddings import FastEmbedProvider


def audit_snapshot(
    *,
    storage_path: Path,
    manifest_path: Path,
    output_path: Path,
) -> dict[str, object]:
    manifest = BootstrapV03Manifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    client = QdrantClient(path=str(storage_path))
    collections = [item.name for item in client.get_collections().collections]
    if collections != [COLLECTION_NAME]:
        raise RuntimeError(f"unexpected local collections: {collections}")
    count = client.count(collection_name=COLLECTION_NAME, exact=True).count
    if count != manifest.expected_point_count:
        raise RuntimeError(
            f"snapshot count {count} does not match manifest {manifest.expected_point_count}"
        )

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
        raise RuntimeError("snapshot retrieval did not return every manifest point")
    checksums = {
        item.knowledge_unit_id: item.payload_checksum
        for item in manifest.projections
    }
    for record in records:
        point_id = str(record.id)
        payload = record.payload or {}
        if (
            payload.get("payload_checksum") != checksums[point_id]
            or payload.get("storage_manifest_id") != manifest.id
            or payload.get("manifest_hash") != manifest.manifest_hash
            or payload.get("bootstrap_generated") is not True
            or payload.get("human_verified") is not False
            or payload.get("review_status") != "bootstrap"
            or payload.get("approved") is not False
            or payload.get("storage_permission") is not False
        ):
            raise RuntimeError(f"unsafe or unbound snapshot payload: {point_id}")

    settings = get_settings()
    embedding = FastEmbedProvider(
        settings.embedding_model,
        settings.embedding_dimension,
    )
    query_results = {}
    expected_ids = set(manifest.expected_point_ids)
    for name, query in QUERIES.items():
        response = client.query_points(
            collection_name=COLLECTION_NAME,
            query=embedding.embed_query(query),
            limit=3,
            with_payload=True,
            with_vectors=False,
        )
        hits = []
        for point in response.points:
            point_id = str(point.id)
            if point_id not in expected_ids:
                raise RuntimeError(f"retrieval returned a stale point: {point_id}")
            payload = point.payload or {}
            if (
                payload.get("bootstrap_generated") is not True
                or payload.get("human_verified") is not False
                or payload.get("review_status") != "bootstrap"
            ):
                raise RuntimeError(f"retrieval returned an unsafe point: {point_id}")
            hits.append(
                {
                    "point_id": point_id,
                    "score": point.score,
                    "title": payload.get("title"),
                    "artifact_type": payload.get("artifact_type"),
                    "knowledge_origin": payload.get("knowledge_origin"),
                }
            )
        query_results[name] = {"query": query, "hits": hits}

    report: dict[str, object] = {
        "schema_version": "bootstrap-snapshot-smoke-0.1",
        "tested_at": datetime.now(UTC).isoformat(),
        "storage_path": storage_path.as_posix(),
        "collection": COLLECTION_NAME,
        "manifest_id": manifest.id,
        "manifest_hash": manifest.manifest_hash,
        "collection_count": count,
        "manifest_point_count": manifest.expected_point_count,
        "all_manifest_points_retrieved": True,
        "all_payload_checksums_match": True,
        "all_payloads_bootstrap_safe": True,
        "production_collection_present": "magicforge_knowledge_v01" in collections,
        "queries": query_results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-path", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_snapshot(
        storage_path=args.storage_path,
        manifest_path=args.manifest,
        output_path=args.output,
    )
    print(
        json.dumps(
            {
                "collection": report["collection"],
                "collection_count": report["collection_count"],
                "queries": len(report["queries"]),
                "all_payloads_bootstrap_safe": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
