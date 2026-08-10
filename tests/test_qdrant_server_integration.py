from __future__ import annotations

import os
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient, models


@pytest.mark.integration
def test_real_qdrant_server_round_trip() -> None:
    """Prove the pinned client can round-trip a governed payload on Qdrant server."""

    url = os.getenv("TEST_QDRANT_URL", "").strip()
    if not url:
        pytest.skip("TEST_QDRANT_URL is not configured; Qdrant integration was not run.")

    collection = f"magicforge_ci_{uuid4().hex}"
    client = QdrantClient(url=url, timeout=10)
    point_id = str(uuid4())
    try:
        client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(
                size=8,
                distance=models.Distance.COSINE,
            ),
        )
        client.upsert(
            collection_name=collection,
            wait=True,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                    payload={
                        "artifact_type": "evidence_card",
                        "bootstrap_generated": True,
                        "human_verified": False,
                        "review_status": "demo",
                    },
                )
            ],
        )
        result = client.query_points(
            collection_name=collection,
            query=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            limit=1,
            with_payload=True,
        )

        assert len(result.points) == 1
        assert str(result.points[0].id) == point_id
        assert result.points[0].payload["artifact_type"] == "evidence_card"
        assert result.points[0].payload["human_verified"] is False
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)
        client.close()
