"""Read-only runtime verification for the public MagicForge Demo stack."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request


DEMO_CORPUS_ID = "magicforge-demo-v01"
DEMO_COLLECTION = "magicforge_demo_v01"
DEMO_SCHEMA = "demo-corpus-0.1"


class DoctorError(RuntimeError):
    """A failed, side-effect-free runtime verification."""


def _read_json_url(url: str) -> dict[str, object]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DoctorError("doctor received an invalid HTTP endpoint")
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.load(response)
    except Exception as exc:
        raise DoctorError(f"runtime endpoint is unavailable: {url}") from exc
    if not isinstance(payload, dict):
        raise DoctorError(f"runtime endpoint returned non-object JSON: {url}")
    return payload


def _demo_identity(corpus_path: Path) -> tuple[str, str, int]:
    try:
        payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DoctorError("the committed public Demo corpus is invalid") from exc
    if not isinstance(payload, dict):
        raise DoctorError("the committed public Demo corpus is not an object")
    for marker in ("synthetic", "self_authored", "redistribution_allowed"):
        if payload.get(marker) is not True:
            raise DoctorError(f"the public Demo corpus lacks {marker}=true")
    corpus_id = str(payload.get("corpus_id") or "")
    collection = str(payload.get("collection_name") or "")
    schema = str(payload.get("schema_version") or "")
    if (
        corpus_id != DEMO_CORPUS_ID
        or collection != DEMO_COLLECTION
        or schema != DEMO_SCHEMA
    ):
        raise DoctorError("the public Demo corpus identity is incompatible")
    point_count = 0
    for key in ("claims", "nodes", "relationships"):
        records = payload.get(key)
        if not isinstance(records, list) or not records:
            raise DoctorError(f"the public Demo corpus has no {key}")
        point_count += len(records)
    return corpus_id, collection, point_count


def _mapping(parent: dict[str, object], key: str) -> dict[str, object]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise DoctorError(f"runtime response lacks object field {key}")
    return value


def verify(*, api_url: str, qdrant_url: str, corpus_path: Path) -> dict[str, object]:
    corpus_id, collection, point_count = _demo_identity(corpus_path)
    readiness = _read_json_url(f"{api_url.rstrip('/')}/health/ready")
    expected_readiness = {
        "status": "ready",
        "mode": "bootstrap",
        "profile": "demo",
        "read_only": True,
        "synthetic_corpus": True,
        "corpus_id": corpus_id,
        "manifest_schema_version": DEMO_SCHEMA,
        "collection": collection,
        "storage_kind": "remote",
        "glm_configured": False,
    }
    for key, expected in expected_readiness.items():
        if readiness.get(key) != expected:
            raise DoctorError(f"FastAPI readiness has unexpected {key}")

    console = _read_json_url(f"{api_url.rstrip('/')}/research/console")
    current_run = _mapping(console, "current_run")
    memory = _mapping(console, "memory_vault")
    runtime = _mapping(console, "runtime")
    manifest = _mapping(memory, "manifest")
    receipt = _mapping(memory, "receipt")
    retrieval = _mapping(runtime, "retrieval")
    intelligence = _mapping(runtime, "intelligence_instrument")
    if (
        current_run.get("run_id") != corpus_id
        or current_run.get("collection") != collection
        or memory.get("runtime_collection") != collection
        or memory.get("audited_collection") != collection
        or manifest.get("point_count") != point_count
        or receipt.get("point_count") != point_count
        or retrieval.get("collection") != collection
        or intelligence.get("provider") != "Offline deterministic Demo"
    ):
        raise DoctorError("research console does not match the public Demo corpus")

    qdrant = _read_json_url(
        f"{qdrant_url.rstrip('/')}/collections/{urllib.parse.quote(collection, safe='')}"
    )
    result = qdrant.get("result")
    if not isinstance(result, dict) or result.get("points_count") != point_count:
        raise DoctorError("server Qdrant point count does not match the public Demo corpus")

    return {
        "status": "ready",
        "profile": "demo",
        "corpus_id": corpus_id,
        "collection": collection,
        "point_count": point_count,
        "private_mounts": 0,
    }


def verify_development(*, api_url: str, qdrant_url: str) -> dict[str, object]:
    """Verify an explicitly configured Development corpus without mutating it."""

    readiness = _read_json_url(f"{api_url.rstrip('/')}/health/ready")
    expected = {
        "status": "ready",
        "mode": "bootstrap",
        "profile": "development",
        "read_only": False,
        "synthetic_corpus": False,
    }
    for key, value in expected.items():
        if readiness.get(key) != value:
            raise DoctorError(f"Development readiness has unexpected {key}")
    corpus_id = str(readiness.get("corpus_id") or "")
    collection = str(readiness.get("collection") or "")
    if not corpus_id or not collection or "demo" in collection.casefold():
        raise DoctorError("Development corpus identity is missing or unsafe")

    console = _read_json_url(f"{api_url.rstrip('/')}/research/console")
    current_run = _mapping(console, "current_run")
    memory = _mapping(console, "memory_vault")
    manifest = _mapping(memory, "manifest")
    receipt = _mapping(memory, "receipt")
    manifest_points = manifest.get("point_count")
    if (
        current_run.get("run_id") != corpus_id
        or current_run.get("collection") != collection
        or memory.get("runtime_collection") != collection
        or not isinstance(manifest_points, int)
        or manifest_points <= 0
        or receipt.get("point_count") != manifest_points
    ):
        raise DoctorError("Development console does not match its active corpus")

    qdrant = _read_json_url(
        f"{qdrant_url.rstrip('/')}/collections/{urllib.parse.quote(collection, safe='')}"
    )
    result = qdrant.get("result")
    if not isinstance(result, dict) or result.get("points_count") != manifest_points:
        raise DoctorError("Development Qdrant point count does not match its manifest")
    return {
        "status": "ready",
        "profile": "development",
        "corpus_id": corpus_id,
        "collection": collection,
        "point_count": manifest_points,
        "private_mounts": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("demo", "development"))
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--qdrant-url", required=True)
    parser.add_argument("--corpus", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.profile == "demo":
            if arguments.corpus is None:
                raise DoctorError("Demo doctor requires --corpus")
            report = verify(
                api_url=arguments.api_url,
                qdrant_url=arguments.qdrant_url,
                corpus_path=arguments.corpus,
            )
        else:
            report = verify_development(
                api_url=arguments.api_url,
                qdrant_url=arguments.qdrant_url,
            )
    except DoctorError as exc:
        print(f"doctor failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
