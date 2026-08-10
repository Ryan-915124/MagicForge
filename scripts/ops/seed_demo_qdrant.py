"""Materialize the public synthetic Demo corpus into server Qdrant.

This command is intentionally limited to the demo collection and to the
``demo``/``development`` Compose profiles. It cannot target Production.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from qdrant_client import QdrantClient

from knowledge.demo import (
    DEMO_COLLECTION_NAME,
    DemoCorpusBundle,
    DemoCorpusError,
    load_demo_bundle,
)
from retrieval.demo import (
    DemoSeedError as RetrievalDemoSeedError,
    DeterministicDemoEmbeddingProvider,
    seed_demo_collection,
)


DEFAULT_CORPUS = Path("/opt/magicforge/data/demo/corpus.json")
ALLOWED_PROFILES = frozenset({"demo", "development"})


class DemoSeedError(RuntimeError):
    """A safe, operator-facing demo seed failure."""


def _profile() -> str:
    profile = os.getenv("MAGICFORGE_TARGET_PROFILE", "").strip().casefold()
    if profile not in ALLOWED_PROFILES:
        raise DemoSeedError(
            "Demo seed requires MAGICFORGE_TARGET_PROFILE=demo or development."
        )
    return profile


def _bundle(path: Path) -> DemoCorpusBundle:
    try:
        bundle = load_demo_bundle(path)
    except DemoCorpusError as exc:
        raise DemoSeedError("The committed public Demo corpus failed validation.") from exc
    if bundle.spec.collection_name != DEMO_COLLECTION_NAME:
        raise DemoSeedError("The public Demo corpus targets an unsafe collection.")
    return bundle


def seed(corpus_path: Path) -> dict[str, object]:
    profile = _profile()
    bundle = _bundle(corpus_path)
    qdrant_url = os.getenv("QDRANT_URL", "").strip()
    if not qdrant_url.startswith(("http://", "https://")):
        raise DemoSeedError("Demo seed requires an HTTP(S) QDRANT_URL.")
    try:
        dimension = int(os.getenv("EMBEDDING_DIMENSION", "384"))
    except ValueError as exc:
        raise DemoSeedError("EMBEDDING_DIMENSION must be an integer.") from exc
    if dimension <= 0:
        raise DemoSeedError("EMBEDDING_DIMENSION must be positive.")

    embedding = DeterministicDemoEmbeddingProvider(dimension)
    client = QdrantClient(url=qdrant_url, timeout=30)
    try:
        client.get_collections()
        try:
            created = seed_demo_collection(client, bundle, embedding)
        except RetrievalDemoSeedError as exc:
            raise DemoSeedError(str(exc)) from exc
        point_count = client.count(
            collection_name=DEMO_COLLECTION_NAME,
            exact=True,
        ).count
        return {
            "status": "seeded",
            "profile": profile,
            "created": created,
            "collection": DEMO_COLLECTION_NAME,
            "manifest_id": bundle.manifest_id,
            "point_count": int(point_count),
        }
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate the profile and manifest without contacting Qdrant.",
    )
    arguments = parser.parse_args()
    try:
        profile = _profile()
        bundle = _bundle(arguments.corpus)
        if arguments.check:
            report = {
                "status": "valid",
                "profile": profile,
                "collection": bundle.spec.collection_name,
                "manifest_id": bundle.manifest_id,
                "point_count": len(bundle.projections),
            }
        else:
            report = seed(arguments.corpus)
    except DemoSeedError as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
