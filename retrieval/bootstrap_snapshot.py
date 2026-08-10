"""Build a validated, isolated Bootstrap v03 snapshot without writing Qdrant.

The snapshot builder is deliberately limited to manifest composition.  It
validates both input manifests, detects point identity conflicts, reapplies the
current Bootstrap v03 storage-safety gate to every input projection, and emits
an immutable lineage record.  Persisting vectors remains a separate, explicit
operation handled by :mod:`retrieval.bootstrap_v03`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from knowledge.bootstrap import BootstrapQdrantProjection, BootstrapStorageManifest
from retrieval.bootstrap_v03 import (
    COLLECTION_NAME,
    BootstrapV03Manifest,
    _storage_safe,
)


LINEAGE_SCHEMA_VERSION = "bootstrap-snapshot-lineage-0.1"
EXCLUSION_SCHEMA_VERSION = "bootstrap-snapshot-exclusions-0.1"
SNAPSHOT_CREATED_BY = "bootstrap-snapshot-merge-v01"


class BootstrapSnapshotError(RuntimeError):
    """Raised when snapshot construction cannot preserve manifest integrity."""


class BootstrapSnapshotPolicyExclusion(BaseModel):
    """One independently audited point quarantined from the snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    point_id: str
    reason: str = Field(min_length=1)
    source_audit: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_point_id(self) -> "BootstrapSnapshotPolicyExclusion":
        UUID(self.point_id)
        return self


class BootstrapSnapshotExclusionLedger(BaseModel):
    """Content-addressed safety decision input; this is not human approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["bootstrap-snapshot-exclusions-0.1"] = (
        EXCLUSION_SCHEMA_VERSION
    )
    run_id: str = Field(min_length=1)
    source_audit_path: str = Field(min_length=1)
    source_audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    human_approval: Literal[False] = False
    exclusions: list[BootstrapSnapshotPolicyExclusion] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "BootstrapSnapshotExclusionLedger":
        ids = [item.point_id for item in self.exclusions]
        if len(ids) != len(set(ids)):
            raise ValueError("snapshot exclusion ledger contains duplicate point IDs")
        return self


class BootstrapSnapshotInput(BaseModel):
    """Identity and safety accounting for one input manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["baseline", "delta"]
    manifest_id: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: str = Field(min_length=1)
    collection_name: str = Field(min_length=1)
    source_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    point_count: int = Field(ge=1)
    storage_safe_point_count: int = Field(ge=0)
    unsafe_input_projection_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> "BootstrapSnapshotInput":
        UUID(self.manifest_id)
        if (
            self.storage_safe_point_count + self.unsafe_input_projection_count
            != self.point_count
        ):
            raise ValueError("snapshot input safety counts do not match point count")
        return self


class BootstrapSnapshotOutput(BaseModel):
    """Content identity of the composed v03 snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_id: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_version: str = Field(min_length=1)
    collection_name: Literal["magicforge_bootstrap_v03"] = COLLECTION_NAME
    point_count: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_manifest_id(self) -> "BootstrapSnapshotOutput":
        UUID(self.manifest_id)
        return self


class BootstrapSnapshotLineage(BaseModel):
    """Deterministic provenance for one baseline-plus-delta composition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    schema_version: Literal["bootstrap-snapshot-lineage-0.1"] = (
        LINEAGE_SCHEMA_VERSION
    )
    lineage_hash: str = ""
    inputs: list[BootstrapSnapshotInput] = Field(min_length=2, max_length=2)
    duplicate_point_count: int = Field(default=0, ge=0)
    duplicate_point_ids: list[str] = Field(default_factory=list)
    conflict_point_count: Literal[0] = 0
    unsafe_input_projection_count: int = Field(default=0, ge=0)
    unsafe_unique_point_count: int = Field(default=0, ge=0)
    excluded_unsafe_point_ids: list[str] = Field(default_factory=list)
    policy_exclusion_ledger_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    policy_exclusions: list[BootstrapSnapshotPolicyExclusion] = Field(
        default_factory=list
    )
    output: BootstrapSnapshotOutput
    created_by: Literal["bootstrap-snapshot-merge-v01"] = SNAPSHOT_CREATED_BY
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def assign_identity(self) -> "BootstrapSnapshotLineage":
        if [item.role for item in self.inputs] != ["baseline", "delta"]:
            raise ValueError("snapshot lineage inputs must be baseline then delta")
        for point_id in self.duplicate_point_ids + self.excluded_unsafe_point_ids:
            UUID(point_id)
        if len(self.duplicate_point_ids) != len(set(self.duplicate_point_ids)):
            raise ValueError("snapshot lineage contains duplicate duplicate-point IDs")
        if len(self.excluded_unsafe_point_ids) != len(
            set(self.excluded_unsafe_point_ids)
        ):
            raise ValueError("snapshot lineage contains duplicate exclusion IDs")
        policy_ids = [item.point_id for item in self.policy_exclusions]
        if len(policy_ids) != len(set(policy_ids)):
            raise ValueError("snapshot lineage contains duplicate policy exclusions")
        if bool(self.policy_exclusions) != bool(
            self.policy_exclusion_ledger_sha256
        ):
            raise ValueError(
                "snapshot policy exclusions require exactly one ledger digest"
            )
        if not set(policy_ids).issubset(self.excluded_unsafe_point_ids):
            raise ValueError("policy exclusion IDs must be absent from the snapshot")
        if self.duplicate_point_count != len(self.duplicate_point_ids):
            raise ValueError("snapshot duplicate count does not match IDs")
        if self.unsafe_unique_point_count != len(self.excluded_unsafe_point_ids):
            raise ValueError("snapshot unsafe count does not match exclusion IDs")
        input_unsafe = sum(item.unsafe_input_projection_count for item in self.inputs)
        if self.unsafe_input_projection_count != input_unsafe:
            raise ValueError("snapshot aggregate unsafe input count is invalid")
        unique_input_count = (
            sum(item.point_count for item in self.inputs)
            - self.duplicate_point_count
        )
        if (
            unique_input_count - self.unsafe_unique_point_count
            != self.output.point_count
        ):
            raise ValueError("snapshot output count does not match merge accounting")

        digest = _json_hash(
            self.model_dump(
                mode="json",
                exclude={"id", "lineage_hash", "created_at"},
            )
        )
        expected_id = str(
            uuid5(NAMESPACE_URL, f"magicforge:bootstrap-snapshot-lineage:{digest}")
        )
        if self.id and self.id != expected_id:
            raise ValueError("snapshot lineage ID does not match contents")
        if self.lineage_hash and self.lineage_hash != digest:
            raise ValueError("snapshot lineage hash does not match contents")
        object.__setattr__(self, "id", expected_id)
        object.__setattr__(self, "lineage_hash", digest)
        return self


class BootstrapSnapshotResult(BaseModel):
    """In-memory snapshot output; it has no Qdrant side effects."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: BootstrapV03Manifest
    lineage: BootstrapSnapshotLineage

    @model_validator(mode="after")
    def validate_binding(self) -> "BootstrapSnapshotResult":
        output = self.lineage.output
        if (
            output.manifest_id != self.manifest.id
            or output.manifest_hash != self.manifest.manifest_hash
            or output.point_count != self.manifest.expected_point_count
            or output.collection_name != self.manifest.collection_name
        ):
            raise ValueError("snapshot lineage output is not bound to its manifest")
        return self


def merge_bootstrap_snapshot_files(
    *,
    baseline_path: Path,
    delta_path: Path,
    policy_exclusions_path: Path | None = None,
) -> BootstrapSnapshotResult:
    """Load, strictly validate, and merge a v03 baseline with a v02 delta."""

    baseline_raw, baseline_bytes = _read_manifest_object(
        baseline_path, role="baseline"
    )
    delta_raw, delta_bytes = _read_manifest_object(delta_path, role="delta")
    baseline = _validate_stored_manifest(
        baseline_raw, BootstrapV03Manifest, role="baseline"
    )
    delta = _validate_stored_manifest(
        delta_raw, BootstrapStorageManifest, role="delta"
    )
    policy_exclusions: Sequence[BootstrapSnapshotPolicyExclusion] = ()
    policy_exclusion_ledger_sha256: str | None = None
    if policy_exclusions_path is not None:
        ledger, ledger_bytes = _read_policy_exclusion_ledger(
            policy_exclusions_path
        )
        policy_exclusions = ledger.exclusions
        policy_exclusion_ledger_sha256 = hashlib.sha256(ledger_bytes).hexdigest()
    return build_bootstrap_snapshot(
        baseline=baseline,
        delta=delta,
        baseline_file_sha256=hashlib.sha256(baseline_bytes).hexdigest(),
        delta_file_sha256=hashlib.sha256(delta_bytes).hexdigest(),
        policy_exclusions=policy_exclusions,
        policy_exclusion_ledger_sha256=policy_exclusion_ledger_sha256,
    )


def build_bootstrap_snapshot(
    *,
    baseline: BootstrapV03Manifest,
    delta: BootstrapStorageManifest,
    baseline_file_sha256: str,
    delta_file_sha256: str,
    policy_exclusions: Sequence[BootstrapSnapshotPolicyExclusion] = (),
    policy_exclusion_ledger_sha256: str | None = None,
) -> BootstrapSnapshotResult:
    """Compose a storage-safe v03 manifest and its exact lineage.

    Point identity conflicts are detected before filtering so an unsafe delta
    cannot conceal a checksum conflict.  Equal point/checksum pairs are
    deduplicated, and the baseline representation wins deterministically.
    """

    _require_sha256(baseline_file_sha256, name="baseline_file_sha256")
    _require_sha256(delta_file_sha256, name="delta_file_sha256")
    if bool(policy_exclusions) != bool(policy_exclusion_ledger_sha256):
        raise BootstrapSnapshotError(
            "policy exclusions require exactly one exclusion-ledger digest"
        )
    if policy_exclusion_ledger_sha256 is not None:
        _require_sha256(
            policy_exclusion_ledger_sha256,
            name="policy_exclusion_ledger_sha256",
        )

    ordered_inputs: list[
        tuple[Literal["baseline", "delta"], BootstrapQdrantProjection]
    ] = [
        *(("baseline", item) for item in baseline.projections),
        *(("delta", item) for item in delta.projections),
    ]

    by_point_id: dict[str, BootstrapQdrantProjection] = {}
    duplicate_point_ids: list[str] = []
    for role, projection in ordered_inputs:
        existing = by_point_id.get(projection.knowledge_unit_id)
        if existing is None:
            by_point_id[projection.knowledge_unit_id] = projection
            continue
        if existing.payload_checksum != projection.payload_checksum:
            raise BootstrapSnapshotError(
                "bootstrap snapshot point checksum conflict for "
                f"{projection.knowledge_unit_id} ({role})"
            )
        duplicate_point_ids.append(projection.knowledge_unit_id)

    # Evaluate every input, including duplicates, to keep per-input safety
    # accounting explicit and prevent a delta from bypassing the v03 gate.
    input_safety: dict[Literal["baseline", "delta"], list[bool]] = {
        "baseline": [],
        "delta": [],
    }
    for role, projection in ordered_inputs:
        input_safety[role].append(_storage_safe(projection))

    storage_unsafe_point_ids = {
        point_id
        for point_id, projection in by_point_id.items()
        if not _storage_safe(projection)
    }
    policy_by_id = {item.point_id: item for item in policy_exclusions}
    if len(policy_by_id) != len(policy_exclusions):
        raise BootstrapSnapshotError("policy exclusions contain duplicate point IDs")
    unknown_policy_ids = sorted(set(policy_by_id) - set(by_point_id))
    if unknown_policy_ids:
        raise BootstrapSnapshotError(
            "policy exclusion IDs are absent from snapshot inputs: "
            + ", ".join(unknown_policy_ids)
        )
    excluded_unsafe_point_ids = sorted(
        storage_unsafe_point_ids | set(policy_by_id)
    )
    excluded = set(excluded_unsafe_point_ids)
    output_projections = sorted(
        (
            projection
            for point_id, projection in by_point_id.items()
            if point_id not in excluded
        ),
        key=lambda item: item.knowledge_unit_id,
    )
    if not output_projections:
        raise BootstrapSnapshotError("bootstrap snapshot has no storage-safe points")
    if any(
        not item.bootstrap_generated
        or item.human_verified
        or item.approved
        or item.storage_permission
        for item in output_projections
    ):
        raise BootstrapSnapshotError(
            "bootstrap snapshot contains unsafe governance markers"
        )

    manifest = BootstrapV03Manifest(
        projections=output_projections,
        created_by=SNAPSHOT_CREATED_BY,
    )
    inputs = [
        _input_lineage(
            role="baseline",
            manifest=baseline,
            source_file_sha256=baseline_file_sha256,
            safety=input_safety["baseline"],
        ),
        _input_lineage(
            role="delta",
            manifest=delta,
            source_file_sha256=delta_file_sha256,
            safety=input_safety["delta"],
        ),
    ]
    lineage = BootstrapSnapshotLineage(
        inputs=inputs,
        duplicate_point_count=len(duplicate_point_ids),
        duplicate_point_ids=sorted(set(duplicate_point_ids)),
        unsafe_input_projection_count=sum(
            item.unsafe_input_projection_count for item in inputs
        ),
        unsafe_unique_point_count=len(excluded_unsafe_point_ids),
        excluded_unsafe_point_ids=excluded_unsafe_point_ids,
        policy_exclusion_ledger_sha256=policy_exclusion_ledger_sha256,
        policy_exclusions=sorted(
            policy_exclusions,
            key=lambda item: item.point_id,
        ),
        output=BootstrapSnapshotOutput(
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            schema_version=manifest.schema_version,
            collection_name=manifest.collection_name,
            point_count=manifest.expected_point_count,
        ),
    )
    return BootstrapSnapshotResult(manifest=manifest, lineage=lineage)


def write_snapshot_artifacts(
    result: BootstrapSnapshotResult,
    *,
    manifest_path: Path,
    lineage_path: Path,
    overwrite: bool = False,
) -> None:
    """Write JSON artifacts only; this function never opens a Qdrant client."""

    if manifest_path.resolve() == lineage_path.resolve():
        raise BootstrapSnapshotError("manifest and lineage paths must differ")
    existing = [path for path in (manifest_path, lineage_path) if path.exists()]
    if existing and not overwrite:
        raise BootstrapSnapshotError(
            "refusing to overwrite snapshot artifacts: "
            + ", ".join(str(path) for path in existing)
        )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    lineage_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(manifest_path, result.manifest.model_dump(mode="json"))
    _write_json(lineage_path, result.lineage.model_dump(mode="json"))


def _input_lineage(
    *,
    role: Literal["baseline", "delta"],
    manifest: BootstrapV03Manifest | BootstrapStorageManifest,
    source_file_sha256: str,
    safety: list[bool],
) -> BootstrapSnapshotInput:
    safe_count = sum(safety)
    return BootstrapSnapshotInput(
        role=role,
        manifest_id=manifest.id,
        manifest_hash=manifest.manifest_hash,
        schema_version=manifest.schema_version,
        collection_name=manifest.collection_name,
        source_file_sha256=source_file_sha256,
        point_count=len(manifest.projections),
        storage_safe_point_count=safe_count,
        unsafe_input_projection_count=len(safety) - safe_count,
    )


def _read_manifest_object(
    path: Path, *, role: Literal["baseline", "delta"]
) -> tuple[dict[str, object], bytes]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BootstrapSnapshotError(f"could not read {role} manifest: {exc}") from exc
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapSnapshotError(f"invalid {role} manifest JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise BootstrapSnapshotError(f"{role} manifest must be a JSON object")
    return raw, data


def _read_policy_exclusion_ledger(
    path: Path,
) -> tuple[BootstrapSnapshotExclusionLedger, bytes]:
    """Read a pinned audit-derived exclusion ledger and verify its source."""

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BootstrapSnapshotError(
            f"could not read policy exclusion ledger: {exc}"
        ) from exc
    try:
        ledger = BootstrapSnapshotExclusionLedger.model_validate_json(data)
    except Exception as exc:
        raise BootstrapSnapshotError(
            f"invalid policy exclusion ledger: {exc}"
        ) from exc

    audit_path = Path(ledger.source_audit_path)
    if not audit_path.is_absolute():
        audit_path = Path.cwd() / audit_path
    try:
        audit_digest = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BootstrapSnapshotError(
            f"could not read exclusion source audit: {exc}"
        ) from exc
    if audit_digest != ledger.source_audit_sha256:
        raise BootstrapSnapshotError(
            "policy exclusion source-audit digest does not match"
        )
    return ledger, data


def _validate_stored_manifest(
    raw: dict[str, object],
    model: type[BootstrapV03Manifest] | type[BootstrapStorageManifest],
    *,
    role: Literal["baseline", "delta"],
) -> BootstrapV03Manifest | BootstrapStorageManifest:
    required = {
        "id",
        "schema_version",
        "manifest_hash",
        "collection_name",
        "projections",
        "expected_point_ids",
        "expected_point_count",
    }
    missing = sorted(required - raw.keys())
    if missing:
        raise BootstrapSnapshotError(
            f"{role} manifest is missing stored identity fields: {', '.join(missing)}"
        )
    projections = raw.get("projections")
    expected_ids = raw.get("expected_point_ids")
    expected_count = raw.get("expected_point_count")
    if not isinstance(projections, list) or not isinstance(expected_ids, list):
        raise BootstrapSnapshotError(
            f"{role} manifest projection and expected-ID fields must be arrays"
        )
    if expected_count != len(projections) or len(expected_ids) != len(projections):
        raise BootstrapSnapshotError(
            f"{role} manifest stored point count does not match projections"
        )
    if not raw.get("id") or not raw.get("manifest_hash"):
        raise BootstrapSnapshotError(f"{role} manifest stored identity is empty")
    try:
        return model.model_validate(raw)
    except Exception as exc:
        raise BootstrapSnapshotError(
            f"{role} manifest failed model/hash validation: {exc}"
        ) from exc


def _require_sha256(value: str, *, name: str) -> None:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise BootstrapSnapshotError(f"{name} must be a lowercase SHA-256 digest")


def _json_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compose Bootstrap manifests without writing Qdrant."
    )
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--delta", type=Path, required=True)
    parser.add_argument("--policy-exclusions", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    result = merge_bootstrap_snapshot_files(
        baseline_path=args.baseline,
        delta_path=args.delta,
        policy_exclusions_path=args.policy_exclusions,
    )
    write_snapshot_artifacts(
        result,
        manifest_path=args.output_dir / "bootstrap-snapshot-manifest-v03.json",
        lineage_path=args.output_dir / "bootstrap-snapshot-lineage.json",
        overwrite=args.overwrite,
    )
    print(
        json.dumps(
            result.lineage.model_dump(mode="json"), ensure_ascii=False, indent=2
        )
    )


__all__ = [
    "BootstrapSnapshotError",
    "BootstrapSnapshotExclusionLedger",
    "BootstrapSnapshotInput",
    "BootstrapSnapshotLineage",
    "BootstrapSnapshotOutput",
    "BootstrapSnapshotResult",
    "BootstrapSnapshotPolicyExclusion",
    "build_bootstrap_snapshot",
    "merge_bootstrap_snapshot_files",
    "write_snapshot_artifacts",
]


if __name__ == "__main__":
    main()
