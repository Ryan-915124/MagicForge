from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from knowledge.bootstrap import (
    BootstrapQdrantProjection,
    BootstrapStorageManifest,
)
from retrieval.bootstrap_snapshot import (
    BootstrapSnapshotPolicyExclusion,
    BootstrapSnapshotError,
    build_bootstrap_snapshot,
    merge_bootstrap_snapshot_files,
    write_snapshot_artifacts,
)
from retrieval.bootstrap_v03 import BootstrapV03Manifest, COLLECTION_NAME


def _projection(
    *,
    artifact_id: str | None = None,
    title: str = "Selective attention",
    role: str = "result",
    sensitivity: str = "controlled",
) -> BootstrapQdrantProjection:
    artifact_id = artifact_id or str(uuid4())
    evidence_id = str(uuid4())
    source_id = str(uuid4())
    return BootstrapQdrantProjection(
        knowledge_unit_id=BootstrapQdrantProjection.id_for_artifact(artifact_id, 1),
        artifact_type="knowledge_node",
        artifact_id=artifact_id,
        knowledge_type="psychology",
        text=(
            "Knowledge type: psychology\n"
            f"Concept: {title}\n"
            "Human verification: false"
        ),
        title=title,
        domain=["theory"],
        ontology_paths=["psychology.attention.selective_attention"],
        knowledge_origin="scientific_evidence",
        evidence_level="empirical",
        evidence_class="controlled_experiment",
        claim_roles=[role],
        confidence=0.7,
        confidence_label="moderate",
        limitations=["Bootstrap extraction has not been human reviewed."],
        source_type="journal_article",
        source_id=source_id,
        citation_id=str(uuid4()),
        source_candidate_id=source_id,
        document_id=source_id,
        source_locator="https://example.org/source",
        supporting_evidence_ids=[evidence_id],
        contradiction_status="not_checked",
        secret_exposure_level="general_principle",
        secret_exposure_rank=1,
        sensitive_information_level=sensitivity,
    )


def _merge(
    baseline_projections: list[BootstrapQdrantProjection],
    delta_projections: list[BootstrapQdrantProjection],
):
    return build_bootstrap_snapshot(
        baseline=BootstrapV03Manifest(projections=baseline_projections),
        delta=BootstrapStorageManifest(projections=delta_projections),
        baseline_file_sha256="a" * 64,
        delta_file_sha256="b" * 64,
    )


def test_snapshot_merges_deduplicates_and_records_lineage() -> None:
    shared = _projection()
    baseline_only = _projection(title="Baseline only")
    delta_only = _projection(title="Delta only")

    result = _merge([shared, baseline_only], [shared, delta_only])

    assert result.manifest.collection_name == COLLECTION_NAME
    assert result.manifest.expected_point_count == 3
    assert result.manifest.expected_point_ids == sorted(
        result.manifest.expected_point_ids
    )
    assert result.lineage.duplicate_point_count == 1
    assert result.lineage.duplicate_point_ids == [shared.knowledge_unit_id]
    assert result.lineage.conflict_point_count == 0
    assert result.lineage.output.manifest_id == result.manifest.id
    assert result.lineage.output.manifest_hash == result.manifest.manifest_hash
    assert [item.role for item in result.lineage.inputs] == ["baseline", "delta"]


def test_snapshot_fails_closed_on_same_point_with_different_checksum() -> None:
    artifact_id = str(uuid4())
    baseline = _projection(artifact_id=artifact_id, title="Original")
    conflicting = baseline.model_copy(update={"title": "Conflicting title"})

    with pytest.raises(BootstrapSnapshotError, match="checksum conflict"):
        _merge([baseline], [conflicting])


def test_snapshot_reapplies_storage_gate_to_every_input() -> None:
    baseline_safe = _projection(title="Safe baseline")
    delta_safe = _projection(title="Safe delta")
    delta_unsafe = _projection(
        title="Controlled method",
        role="method",
        sensitivity="controlled",
    )

    result = _merge([baseline_safe], [delta_safe, delta_unsafe])

    assert result.manifest.expected_point_count == 2
    assert delta_unsafe.knowledge_unit_id not in result.manifest.expected_point_ids
    assert result.lineage.unsafe_input_projection_count == 1
    assert result.lineage.unsafe_unique_point_count == 1
    assert result.lineage.excluded_unsafe_point_ids == [
        delta_unsafe.knowledge_unit_id
    ]
    assert result.lineage.inputs[1].unsafe_input_projection_count == 1


def test_snapshot_applies_explicit_audit_policy_exclusion() -> None:
    baseline_safe = _projection(title="Safe baseline")
    quarantined = _projection(title="Audited method detail")
    delta_safe = _projection(title="Safe delta")

    result = build_bootstrap_snapshot(
        baseline=BootstrapV03Manifest(
            projections=[baseline_safe, quarantined]
        ),
        delta=BootstrapStorageManifest(projections=[delta_safe]),
        baseline_file_sha256="a" * 64,
        delta_file_sha256="b" * 64,
        policy_exclusions=[
            BootstrapSnapshotPolicyExclusion(
                point_id=quarantined.knowledge_unit_id,
                reason="Audited as potential method detail.",
                source_audit="projection-quality-audit.json",
            )
        ],
        policy_exclusion_ledger_sha256="c" * 64,
    )

    assert result.manifest.expected_point_count == 2
    assert quarantined.knowledge_unit_id not in result.manifest.expected_point_ids
    assert result.lineage.policy_exclusion_ledger_sha256 == "c" * 64
    assert result.lineage.policy_exclusions[0].point_id == (
        quarantined.knowledge_unit_id
    )


def test_snapshot_rejects_unknown_policy_exclusion() -> None:
    unknown_id = str(uuid4())

    with pytest.raises(BootstrapSnapshotError, match="absent from snapshot inputs"):
        build_bootstrap_snapshot(
            baseline=BootstrapV03Manifest(projections=[_projection()]),
            delta=BootstrapStorageManifest(projections=[_projection()]),
            baseline_file_sha256="a" * 64,
            delta_file_sha256="b" * 64,
            policy_exclusions=[
                BootstrapSnapshotPolicyExclusion(
                    point_id=unknown_id,
                    reason="Unknown audited point.",
                    source_audit="projection-quality-audit.json",
                )
            ],
            policy_exclusion_ledger_sha256="c" * 64,
        )


def test_file_merge_rejects_tampered_stored_manifest_hash(tmp_path: Path) -> None:
    baseline = BootstrapV03Manifest(projections=[_projection()])
    delta = BootstrapStorageManifest(projections=[_projection()])
    baseline_raw = baseline.model_dump(mode="json")
    baseline_raw["manifest_hash"] = "0" * 64
    baseline_path = tmp_path / "baseline.json"
    delta_path = tmp_path / "delta.json"
    baseline_path.write_text(json.dumps(baseline_raw), encoding="utf-8")
    delta_path.write_text(delta.model_dump_json(), encoding="utf-8")

    with pytest.raises(BootstrapSnapshotError, match="model/hash validation"):
        merge_bootstrap_snapshot_files(
            baseline_path=baseline_path,
            delta_path=delta_path,
        )


def test_file_merge_requires_explicit_stored_identity(tmp_path: Path) -> None:
    baseline = BootstrapV03Manifest(projections=[_projection()])
    delta = BootstrapStorageManifest(projections=[_projection()])
    baseline_raw = baseline.model_dump(mode="json")
    baseline_raw.pop("manifest_hash")
    baseline_path = tmp_path / "baseline.json"
    delta_path = tmp_path / "delta.json"
    baseline_path.write_text(json.dumps(baseline_raw), encoding="utf-8")
    delta_path.write_text(delta.model_dump_json(), encoding="utf-8")

    with pytest.raises(BootstrapSnapshotError, match="missing stored identity"):
        merge_bootstrap_snapshot_files(
            baseline_path=baseline_path,
            delta_path=delta_path,
        )


def test_file_merge_verifies_policy_exclusion_audit_digest(tmp_path: Path) -> None:
    quarantined = _projection(title="Audited method detail")
    baseline = BootstrapV03Manifest(projections=[quarantined, _projection()])
    delta = BootstrapStorageManifest(projections=[_projection()])
    baseline_path = tmp_path / "baseline.json"
    delta_path = tmp_path / "delta.json"
    audit_path = tmp_path / "audit.json"
    ledger_path = tmp_path / "exclusions.json"
    baseline_path.write_text(baseline.model_dump_json(), encoding="utf-8")
    delta_path.write_text(delta.model_dump_json(), encoding="utf-8")
    audit_path.write_text('{"decision":"quarantine"}\n', encoding="utf-8")
    ledger_path.write_text(
        json.dumps(
            {
                "schema_version": "bootstrap-snapshot-exclusions-0.1",
                "run_id": "test-run",
                "source_audit_path": str(audit_path),
                "source_audit_sha256": "0" * 64,
                "human_approval": False,
                "exclusions": [
                    {
                        "point_id": quarantined.knowledge_unit_id,
                        "reason": "Audited as method detail.",
                        "source_audit": audit_path.name,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BootstrapSnapshotError, match="digest does not match"):
        merge_bootstrap_snapshot_files(
            baseline_path=baseline_path,
            delta_path=delta_path,
            policy_exclusions_path=ledger_path,
        )


def test_artifact_writer_is_json_only_and_refuses_overwrite(tmp_path: Path) -> None:
    result = _merge([_projection()], [_projection()])
    manifest_path = tmp_path / "snapshot" / "manifest.json"
    lineage_path = tmp_path / "snapshot" / "lineage.json"

    write_snapshot_artifacts(
        result,
        manifest_path=manifest_path,
        lineage_path=lineage_path,
    )

    stored_manifest = BootstrapV03Manifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    assert stored_manifest.id == result.manifest.id
    assert json.loads(lineage_path.read_text(encoding="utf-8"))["output"][
        "manifest_hash"
    ] == result.manifest.manifest_hash
    with pytest.raises(BootstrapSnapshotError, match="refusing to overwrite"):
        write_snapshot_artifacts(
            result,
            manifest_path=manifest_path,
            lineage_path=lineage_path,
        )
