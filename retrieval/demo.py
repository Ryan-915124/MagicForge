"""Deterministic, offline retrieval support for the public Demo profile."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import NoReturn

from qdrant_client import QdrantClient, models

from knowledge.demo import DemoCorpusBundle
from knowledge.projections import StorageManifest
from retrieval.embeddings import EmbeddingError
from retrieval.qdrant_service import QdrantService, QdrantServiceError


class DemoSeedError(RuntimeError):
    """Raised when an in-memory Demo collection violates its fixed contract."""


class ReadOnlyDemoQdrantService(QdrantService):
    """Qdrant reader whose public mutation surfaces always fail closed."""

    def create_collection(self) -> NoReturn:
        raise QdrantServiceError("the Demo collection is read-only")

    def add_documents(self, documents: Sequence[object]) -> NoReturn:
        del documents
        raise QdrantServiceError("the Demo collection is read-only")

    def write_manifest(
        self,
        manifest: StorageManifest,
        *,
        actor: str,
    ) -> NoReturn:
        del manifest, actor
        raise QdrantServiceError("the Demo collection is read-only")

    def cleanup_manifest_points(self, manifest: StorageManifest) -> NoReturn:
        del manifest
        raise QdrantServiceError("the Demo collection is read-only")


class DeterministicDemoEmbeddingProvider:
    """Small feature-hashing embedder used only for the synthetic Demo corpus.

    It downloads no model, reads no environment credential, and performs no
    network I/O.  It is intentionally not a production-quality embedding
    model; its purpose is to make the public example reproducible and offline.
    """

    def __init__(self, dimension: int) -> None:
        if dimension < 8:
            raise ValueError("Demo embedding dimension must be at least 8")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        tokens = re.findall(r"[\w-]+", text.casefold(), flags=re.UNICODE)
        if not tokens:
            tokens = ["<empty>"]
        features = [*tokens, *(f"{left}:{right}" for left, right in zip(tokens, tokens[1:]))]
        vector = [0.0] * self.dimension
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:  # pragma: no cover - a non-empty feature always has weight
            raise EmbeddingError("Demo embedding unexpectedly produced a zero vector")
        return [value / norm for value in vector]


def seed_demo_collection(
    client: QdrantClient,
    bundle: DemoCorpusBundle,
    embedding_provider: DeterministicDemoEmbeddingProvider,
) -> bool:
    """Seed a process-local collection once, or verify an identical seed.

    Returns ``True`` when the collection was created and ``False`` when the
    exact deterministic contents were already present.  Existing but
    incompatible data is never overwritten.
    """

    name = bundle.spec.collection_name
    if client.collection_exists(name):
        _validate_existing_demo_collection(client, bundle, embedding_provider)
        return False

    client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(
            size=embedding_provider.dimension,
            distance=models.Distance.COSINE,
        ),
    )
    vectors = embedding_provider.embed_documents(
        [projection.text for projection in bundle.projections]
    )
    payloads = bundle.payloads()
    try:
        client.upsert(
            collection_name=name,
            points=[
                models.PointStruct(
                    id=projection.knowledge_unit_id,
                    vector=vector,
                    payload=payload,
                )
                for projection, vector, payload in zip(
                    bundle.projections,
                    vectors,
                    payloads,
                    strict=True,
                )
            ],
            wait=True,
        )
        _validate_existing_demo_collection(client, bundle, embedding_provider)
    except Exception as exc:
        raise DemoSeedError("the in-memory Demo corpus could not be seeded") from exc
    return True


def _validate_existing_demo_collection(
    client: QdrantClient,
    bundle: DemoCorpusBundle,
    embedding_provider: DeterministicDemoEmbeddingProvider,
) -> None:
    name = bundle.spec.collection_name
    try:
        collection = client.get_collection(name)
        vectors = collection.config.params.vectors
        if (
            getattr(vectors, "size", None) != embedding_provider.dimension
            or getattr(vectors, "distance", None) != models.Distance.COSINE
        ):
            raise DemoSeedError("the existing Demo collection has incompatible vectors")
        count = client.count(collection_name=name, exact=True).count
        if count != len(bundle.projections):
            raise DemoSeedError("the existing Demo collection has an unexpected size")
        records = client.retrieve(
            collection_name=name,
            ids=list(bundle.expected_point_ids),
            with_payload=True,
            with_vectors=False,
        )
    except DemoSeedError:
        raise
    except Exception as exc:
        raise DemoSeedError("the existing Demo collection could not be verified") from exc

    if {str(record.id) for record in records} != set(bundle.expected_point_ids):
        raise DemoSeedError("the existing Demo collection has unexpected point identities")
    checksums = bundle.payload_checksums
    for record in records:
        point_id = str(record.id)
        payload = record.payload or {}
        if (
            payload.get("payload_checksum") != checksums[point_id]
            or payload.get("storage_manifest_id") != bundle.manifest_id
            or payload.get("manifest_hash") != bundle.manifest_hash
            or payload.get("bootstrap_generated") is not True
            or payload.get("human_verified") is not False
            or payload.get("review_status") != "bootstrap"
        ):
            raise DemoSeedError("the existing Demo collection payload is incompatible")


__all__ = [
    "DemoSeedError",
    "DeterministicDemoEmbeddingProvider",
    "ReadOnlyDemoQdrantService",
    "seed_demo_collection",
]
