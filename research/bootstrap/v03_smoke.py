"""Read-only retrieval smoke test for the Run 002 local Qdrant collection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from qdrant_client import QdrantClient, models

from app.config import get_settings
from retrieval.bootstrap_v03 import COLLECTION_NAME
from retrieval.embeddings import FastEmbedProvider


QUERIES = {
    "attention_misdirection": "Why did spectators fail to notice a secret action during a magic performance?",
    "temporal_memory": "How can delay and memory reconstruction support temporal misdirection?",
    "emotion_narrative": "How do emotion, humor, tension, and storytelling shape audience experience?",
    "expertise_training": "How should a magician practice to develop expertise and reliable performance skill?",
    "history": "How did stage magic and influential performers develop historically?",
    "card_technique": "What theory supports natural timing and deceptive handling in false shuffles?",
    "mentalism": "How do framing, choice, and dual reality operate in mentalism?",
}


def main() -> None:
    settings = get_settings()
    path = Path("research/runs/bootstrap-002/qdrant_storage_v03")
    output = Path("research/runs/bootstrap-002/reports/retrieval-smoke-test.json")
    client = QdrantClient(path=str(path))
    collections = [item.name for item in client.get_collections().collections]
    if collections != [COLLECTION_NAME]:
        raise RuntimeError(f"unexpected local collections: {collections}")
    embedding = FastEmbedProvider(settings.embedding_model, settings.embedding_dimension)
    results = {}
    for name, query in QUERIES.items():
        vector = embedding.embed_query(query)
        response = client.query_points(
            collection_name=COLLECTION_NAME,
            query=vector,
            limit=3,
            with_payload=True,
            with_vectors=False,
        )
        hits = []
        for point in response.points:
            payload = point.payload or {}
            if (
                payload.get("bootstrap_generated") is not True
                or payload.get("human_verified") is not False
                or payload.get("review_status") != "bootstrap"
            ):
                raise RuntimeError(f"unsafe retrieval payload: {point.id}")
            hits.append(
                {
                    "point_id": str(point.id),
                    "score": point.score,
                    "title": payload.get("title"),
                    "artifact_type": payload.get("artifact_type"),
                    "knowledge_type": payload.get("knowledge_type"),
                    "knowledge_origin": payload.get("knowledge_origin"),
                    "domain": payload.get("domain"),
                    "human_verified": payload.get("human_verified"),
                    "review_status": payload.get("review_status"),
                }
            )
        results[name] = {"query": query, "hits": hits}
    excluded = client.count(
        collection_name=COLLECTION_NAME,
        count_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="source_candidate_id",
                    match=models.MatchValue(
                        value="145054b4-a90a-551c-a73e-90a47c55c5b4"
                    ),
                )
            ]
        ),
        exact=True,
    ).count
    if excluded:
        raise RuntimeError("safety-excluded source is present in v03")
    report = {
        "tested_at": datetime.now(UTC).isoformat(),
        "collection": COLLECTION_NAME,
        "collection_count": client.count(
            collection_name=COLLECTION_NAME, exact=True
        ).count,
        "queries": results,
        "safety_excluded_points_found": excluded,
        "all_returned_hits_bootstrap_safe": True,
        "production_collection_present": "magicforge_knowledge_v01" in collections,
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({
        "collection_count": report["collection_count"],
        "queries": len(results),
        "safety_excluded_points_found": excluded,
        "all_returned_hits_bootstrap_safe": True,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
