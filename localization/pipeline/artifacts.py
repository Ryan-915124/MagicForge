"""Safe, deterministic artifact writers for localization runs."""

from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel


RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
RUN_ARTIFACT_NAMES = (
    "manifest.json",
    "inventory.json",
    "inventory-summary.json",
    "source-units.jsonl",
    "hardcoded-candidates.jsonl",
    "validation-report.json",
    "summary.md",
    "proposals.jsonl",
    "final-proposals.jsonl",
    "review-decisions.jsonl",
    "review-queue.jsonl",
    "final-validation-report.json",
    "failure.json",
)


class ArtifactError(RuntimeError):
    """Raised when a run artifact cannot be created safely."""


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_run_id(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz").casefold()
    return f"{prefix}-{stamp}"


def prepare_run_directory(
    output_root: Path,
    run_id: str,
    *,
    resume: bool = False,
) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ArtifactError(
            "run id must use lowercase letters, digits, dots, underscores, or hyphens"
        )
    output_root.mkdir(parents=True, exist_ok=True)
    run_directory = output_root / run_id
    if run_directory.exists() and not resume:
        raise ArtifactError(
            f"run directory already exists: {run_directory}; use --resume to reuse it"
        )
    run_directory.mkdir(parents=False, exist_ok=resume)
    return run_directory


def isolate_existing_run_artifacts(run_directory: Path) -> Path | None:
    """Move a previous attempt's named artifacts out of the active run root."""

    existing = [run_directory / name for name in RUN_ARTIFACT_NAMES if (run_directory / name).is_file()]
    if not existing:
        return None

    attempts_directory = run_directory / "attempts"
    attempts_directory.mkdir(parents=True, exist_ok=True)
    attempt_id = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S%fZ").casefold()
    archived_attempt = attempts_directory / attempt_id
    moved: list[tuple[Path, Path]] = []
    try:
        archived_attempt.mkdir(parents=False, exist_ok=False)
        for artifact in existing:
            archived = archived_attempt / artifact.name
            os.replace(artifact, archived)
            moved.append((artifact, archived))
    except OSError as exc:
        rollback_errors: list[str] = []
        for original, archived in reversed(moved):
            try:
                if archived.exists() and not original.exists():
                    os.replace(archived, original)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        try:
            if archived_attempt.exists() and not any(archived_attempt.iterdir()):
                archived_attempt.rmdir()
        except OSError as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        rollback_note = (
            "; rollback also failed: " + "; ".join(rollback_errors)
            if rollback_errors
            else ""
        )
        raise ArtifactError(
            f"could not isolate prior run artifacts in {run_directory}: {exc}{rollback_note}"
        ) from exc
    return archived_attempt


def fingerprint_run_artifacts(run_directory: Path) -> dict[str, dict[str, Any]]:
    """Fingerprint every completed-run artifact except the commit manifest itself."""

    fingerprints: dict[str, dict[str, Any]] = {}
    for name in RUN_ARTIFACT_NAMES:
        if name == "manifest.json":
            continue
        path = run_directory / name
        if not path.is_file():
            continue
        payload = path.read_bytes()
        fingerprints[name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
    return fingerprints


def write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(_jsonable(value), ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path: Path, values: Iterable[Any]) -> None:
    payload = "".join(
        json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":")) + "\n"
        for value in values
    )
    _atomic_write(path, payload)


def write_text(path: Path, value: str) -> None:
    _atomic_write(path, value if value.endswith("\n") else value + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ArtifactError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ArtifactError(f"JSONL record at {path}:{line_number} must be an object")
        output.append(value)
    return output


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    except OSError as exc:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise ArtifactError(f"could not write artifact {path}: {exc}") from exc


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value
