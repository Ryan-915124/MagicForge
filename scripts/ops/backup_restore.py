"""Guarded backup and restore for the local public Demo Compose profile."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.parse
import urllib.request

from qdrant_client import QdrantClient
from sqlalchemy.engine import make_url


ALLOWED_PROFILES = frozenset({"demo"})
DEMO_COLLECTION = "magicforge_demo_v01"
BACKUP_ROOT = Path("/backups")


class OpsSafetyError(RuntimeError):
    """An operation was unsafe or incompletely authorized."""


def _profile(operation: str) -> str:
    configured_operation = os.getenv("MAGICFORGE_OPS_OPERATION", "").strip()
    if configured_operation != operation:
        raise OpsSafetyError("The Compose operation profile does not match the command.")
    profile = os.getenv("MAGICFORGE_TARGET_PROFILE", "").strip().casefold()
    if profile not in ALLOWED_PROFILES:
        raise OpsSafetyError("Backup/restore is limited to the public Demo profile.")
    return profile


def _database_command(program: str, *arguments: str) -> tuple[list[str], dict[str, str]]:
    configured = os.getenv("DATABASE_URL", "").strip()
    try:
        url = make_url(configured)
    except Exception as exc:
        raise OpsSafetyError("DATABASE_URL is invalid.") from exc
    if url.get_backend_name() != "postgresql" or not url.host or not url.database:
        raise OpsSafetyError("Backup/restore requires PostgreSQL.")
    environment = dict(os.environ)
    environment["PGPASSWORD"] = url.password or ""
    command = [
        program,
        "--host",
        url.host,
        "--port",
        str(url.port or 5432),
        "--username",
        url.username or "",
        "--dbname",
        url.database,
        *arguments,
    ]
    return command, environment


def _run_database(program: str, *arguments: str) -> None:
    command, environment = _database_command(program, *arguments)
    try:
        subprocess.run(
            command,
            env=environment,
            check=True,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise OpsSafetyError(f"{program} failed.") from exc


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _qdrant() -> tuple[QdrantClient, str, str]:
    url = os.getenv("QDRANT_URL", "").strip().rstrip("/")
    collection = os.getenv("QDRANT_COLLECTION_NAME", "").strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OpsSafetyError("QDRANT_URL must be HTTP(S).")
    if collection != DEMO_COLLECTION:
        raise OpsSafetyError("Backup/restore cannot target a Production collection.")
    return QdrantClient(url=url, timeout=60), url, collection


def backup() -> Path:
    profile = _profile("backup")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    bundle = BACKUP_ROOT / f"magicforge-{profile}-{stamp}"
    if bundle.exists():
        raise OpsSafetyError("The backup bundle already exists.")
    bundle.mkdir(mode=0o700, parents=True)
    database_dump = bundle / "postgres.dump"
    qdrant_dump = bundle / "qdrant.snapshot"
    client: QdrantClient | None = None
    snapshot_name = ""
    try:
        _run_database(
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            f"--file={database_dump}",
        )
        client, qdrant_url, collection = _qdrant()
        snapshot = client.create_snapshot(collection_name=collection, wait=True)
        snapshot_name = str(getattr(snapshot, "name", "") or "")
        if not snapshot_name:
            raise OpsSafetyError("Qdrant did not return a snapshot identity.")
        encoded_collection = urllib.parse.quote(collection, safe="")
        encoded_snapshot = urllib.parse.quote(snapshot_name, safe="")
        request = urllib.request.Request(
            f"{qdrant_url}/collections/{encoded_collection}/snapshots/{encoded_snapshot}"
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            with qdrant_dump.open("wb") as stream:
                shutil.copyfileobj(response, stream)
        point_count = client.count(collection_name=collection, exact=True).count
        metadata = {
            "schema": "magicforge-local-backup-0.1",
            "created_at": datetime.now(UTC).isoformat(),
            "profile": profile,
            "collection": collection,
            "qdrant_point_count": int(point_count),
            "postgres_sha256": _sha256(database_dump),
            "qdrant_sha256": _sha256(qdrant_dump),
        }
        metadata_path = bundle / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for path in (database_dump, qdrant_dump, metadata_path):
            path.chmod(0o600)
        return bundle
    except Exception:
        shutil.rmtree(bundle, ignore_errors=True)
        raise
    finally:
        if client is not None:
            if snapshot_name:
                try:
                    client.delete_snapshot(
                        collection_name=DEMO_COLLECTION,
                        snapshot_name=snapshot_name,
                        wait=True,
                    )
                except Exception:
                    pass
            client.close()


def _restore_bundle(profile: str) -> tuple[Path, dict[str, object]]:
    source = os.getenv("MAGICFORGE_RESTORE_SOURCE", "").strip()
    if not source or Path(source).name != source:
        raise OpsSafetyError("Restore source must be one backup bundle name.")
    bundle = (BACKUP_ROOT / source).resolve()
    if not bundle.is_relative_to(BACKUP_ROOT.resolve()) or not bundle.is_dir():
        raise OpsSafetyError("Restore bundle is unavailable.")
    metadata_path = bundle / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OpsSafetyError("Restore metadata is invalid.") from exc
    if (
        metadata.get("schema") != "magicforge-local-backup-0.1"
        or metadata.get("profile") != profile
        or metadata.get("collection") != DEMO_COLLECTION
    ):
        raise OpsSafetyError("Restore metadata does not match the target profile.")
    database_dump = bundle / "postgres.dump"
    qdrant_dump = bundle / "qdrant.snapshot"
    if not database_dump.is_file() or not qdrant_dump.is_file():
        raise OpsSafetyError("Restore bundle is incomplete.")
    if (
        _sha256(database_dump) != metadata.get("postgres_sha256")
        or _sha256(qdrant_dump) != metadata.get("qdrant_sha256")
    ):
        raise OpsSafetyError("Restore bundle checksum validation failed.")
    return bundle, metadata


def restore() -> Path:
    profile = _profile("restore")
    expected_confirmation = f"restore-{profile}"
    if os.getenv("MAGICFORGE_RESTORE_CONFIRM", "") != expected_confirmation:
        raise OpsSafetyError(
            f"Restore requires MAGICFORGE_RESTORE_CONFIRM={expected_confirmation}."
        )
    bundle, metadata = _restore_bundle(profile)
    database_dump = bundle / "postgres.dump"
    qdrant_dump = bundle / "qdrant.snapshot"
    _run_database(
        "pg_restore",
        "--clean",
        "--if-exists",
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
        "--no-acl",
        str(database_dump),
    )
    client, _qdrant_url, collection = _qdrant()
    try:
        with qdrant_dump.open("rb") as stream:
            client.http.snapshots_api.recover_from_uploaded_snapshot(
                collection_name=collection,
                wait=True,
                snapshot=stream,
            )
        point_count = client.count(collection_name=collection, exact=True).count
        if int(point_count) != int(metadata["qdrant_point_count"]):
            raise OpsSafetyError("Restored Qdrant point count is inconsistent.")
    finally:
        client.close()
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "restore"))
    arguments = parser.parse_args()
    try:
        result = backup() if arguments.operation == "backup" else restore()
    except OpsSafetyError as exc:
        print(json.dumps({"status": "failed", "message": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "status": "completed",
                "operation": arguments.operation,
                "bundle": result.name,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
