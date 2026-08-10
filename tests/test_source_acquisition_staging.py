"""Tests for the offline Corpus Run 001 Source-acquisition boundary."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from research.acquisition.staging import (
    EXPECTED_CANDIDATE_IDS,
    AcquisitionStagingError,
    ExactContentReceipt,
    build_acquisition_manifest,
    build_production_submission,
    write_acquisition_manifest,
)
from research.review.workflow_models import SourceVersionCommand


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_RUN = REPOSITORY_ROOT / "research/runs/magicforge-corpus-run-001"


def _copy_source_run(tmp_path: Path) -> Path:
    destination = tmp_path / "magicforge-corpus-run-001"
    shutil.copytree(SOURCE_RUN, destination)
    return destination


def _manifest_entry(
    manifest: dict, *, unresolved_source_type: bool = False
) -> dict:
    for entry in manifest["entries"]:
        if entry["acquisition"]["status"] != "ready":
            continue
        unresolved = entry["classification"]["source_type"] is None
        if unresolved == unresolved_source_type:
            return entry
    raise AssertionError("test fixture has no matching manifest entry")


def _stage_receipt(
    staging_root: Path,
    entry: dict,
    content: str,
) -> ExactContentReceipt:
    content_path = staging_root / entry["exact_content"]["content_file"]
    content_path.parent.mkdir(parents=True, exist_ok=True)
    raw = content.encode("utf-8")
    content_path.write_bytes(raw)
    locator = entry["acquisition"]["content_locators"][0]
    return ExactContentReceipt(
        candidate_id=entry["candidate_id"],
        content_file=entry["exact_content"]["content_file"],
        content_access=locator["content_access_hint"],
        source_locator=locator["url"],
        acquisition_method="offline-test-fixture",
        content_sha256=hashlib.sha256(raw).hexdigest(),
        content_bytes=len(raw),
        provider_response_saved_at=datetime(2026, 8, 8, tzinfo=UTC),
        returned_url=locator["url"],
        access={
            "access_method": "offline test fixture",
            "license_name": locator["license_name"],
            "redistribution_allowed": False,
        },
    )


def test_manifest_locks_exact_run001_set_and_reports_gates() -> None:
    manifest = build_acquisition_manifest(SOURCE_RUN)

    assert manifest["schema_version"] == "source-acquisition-manifest-0.1"
    assert manifest["source_run_id"] == "magicforge-corpus-run-001"
    assert manifest["summary"] == {
        "candidate_count": 44,
        "category_counts": {"academic": 32, "practitioner": 12},
        "acquisition_ready": 30,
        "access_unresolved": 14,
        "exact_content_missing": 44,
        "production_payload_ready": 0,
        "manual_source_type_required": 1,
    }
    assert {entry["candidate_id"] for entry in manifest["entries"]} == set(
        EXPECTED_CANDIDATE_IDS
    )
    assert [entry["candidate_id"] for entry in manifest["entries"]] == sorted(
        EXPECTED_CANDIDATE_IDS
    )
    assert len(manifest["inputs"]) == 4

    unhashed = dict(manifest)
    recorded_hash = unhashed.pop("manifest_sha256")
    canonical = json.dumps(
        unhashed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert recorded_hash == hashlib.sha256(canonical).hexdigest()


def test_manifest_never_contains_content_or_writable_governance_state() -> None:
    manifest = build_acquisition_manifest(SOURCE_RUN)

    assert manifest["constraints"]["bootstrap_content_fallback"] is False
    assert manifest["constraints"]["api_submission"] is False
    for entry in manifest["entries"]:
        assert entry["exact_content"]["status"] == "missing"
        assert entry["exact_content"]["content_sha256"] is None
        assert entry["production"]["payload_eligible"] is False
        assert entry["production"]["requested_extraction_permission"] == "none"
        assert entry["production"]["requested_storage_permission"] == "none"
        assert entry["production"]["sensitive_information_level"] == "controlled"
        assert entry["production"]["citation_verification"] == []
        assert "content" not in entry
        assert "payload" not in entry["production"]
        assert "approval" not in entry["production"]
        assert "review_status" not in entry["production"]


def test_manifest_identity_matches_future_production_source_id() -> None:
    manifest = build_acquisition_manifest(SOURCE_RUN)

    for entry in manifest["entries"]:
        assert entry["expected_production_source_id"] == entry["candidate_id"]
        if entry["source_category"] == "academic":
            assert entry["identity"]["kind"] == "doi"
            assert entry["identity"]["canonical_key"].startswith("doi:")
            assert entry["classification"]["knowledge_origin"] == (
                "scientific_evidence"
            )
        else:
            assert entry["identity"]["kind"] == "url"
            assert entry["identity"]["canonical_key"].startswith("url-sha256:")
            assert entry["classification"]["knowledge_origin"] == "expert_practice"


def test_manifest_serialization_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    write_acquisition_manifest(SOURCE_RUN, first)
    write_acquisition_manifest(SOURCE_RUN, second)

    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize("field", ["content", "snippet"])
def test_candidate_embedded_text_cannot_be_exact_content_fallback(
    tmp_path: Path, field: str
) -> None:
    copied = _copy_source_run(tmp_path)
    path = copied / "candidates/academic-candidates.json"
    candidates = json.loads(path.read_text(encoding="utf-8"))
    candidates[0][field] = "Bootstrap-derived text must not cross this boundary."
    path.write_text(json.dumps(candidates), encoding="utf-8")

    with pytest.raises(AcquisitionStagingError) as caught:
        build_acquisition_manifest(copied)

    assert caught.value.code == "candidate_embedded_content_forbidden"


def test_replacing_one_record_in_a_same_size_set_is_rejected(tmp_path: Path) -> None:
    copied = _copy_source_run(tmp_path)
    path = copied / "candidates/academic-candidates.json"
    candidates = json.loads(path.read_text(encoding="utf-8"))
    candidates[0]["id"] = str(uuid4())
    path.write_text(json.dumps(candidates), encoding="utf-8")

    with pytest.raises(AcquisitionStagingError) as caught:
        build_acquisition_manifest(copied)

    assert caught.value.code == "locked_candidate_set_mismatch"


def test_production_compilation_requires_a_receipt_and_exact_content(
    tmp_path: Path,
) -> None:
    entry = _manifest_entry(build_acquisition_manifest(SOURCE_RUN))

    with pytest.raises(AcquisitionStagingError) as no_receipt:
        build_production_submission(entry, None, tmp_path)
    assert no_receipt.value.code == "exact_content_receipt_required"

    content = "   \n"
    receipt = _stage_receipt(tmp_path, entry, content)
    with pytest.raises(AcquisitionStagingError) as blank:
        build_production_submission(entry, receipt, tmp_path)
    assert blank.value.code == "exact_content_empty"


def test_production_compilation_checks_content_hash(tmp_path: Path) -> None:
    entry = _manifest_entry(build_acquisition_manifest(SOURCE_RUN))
    receipt = _stage_receipt(tmp_path, entry, "Exact acquired source text.")
    changed = receipt.model_copy(update={"content_sha256": "0" * 64})

    with pytest.raises(AcquisitionStagingError) as caught:
        build_production_submission(entry, changed, tmp_path)

    assert caught.value.code == "content_hash_mismatch"


def test_exact_content_receipt_requires_timezone_aware_saved_at(
    tmp_path: Path,
) -> None:
    entry = _manifest_entry(build_acquisition_manifest(SOURCE_RUN))
    receipt = _stage_receipt(tmp_path, entry, "Exact acquired source text.")
    payload = receipt.model_dump(mode="json")
    payload["provider_response_saved_at"] = "2026-08-08T12:00:00"

    with pytest.raises(ValueError, match="must include a timezone"):
        ExactContentReceipt.model_validate(payload)


def test_valid_exact_content_compiles_safe_source_command(tmp_path: Path) -> None:
    entry = _manifest_entry(build_acquisition_manifest(SOURCE_RUN))
    content = "Exact acquired source text with a stable locator and checksum."
    receipt = _stage_receipt(tmp_path, entry, content)

    prepared = build_production_submission(entry, receipt, tmp_path)
    command = SourceVersionCommand.model_validate(prepared["payload"])

    assert command.content == content
    assert command.requested_extraction_permission.value == "none"
    assert command.requested_storage_permission.value == "none"
    assert command.sensitive_information_level.value == "controlled"
    assert command.citation_verification == []
    assert prepared["candidate_id"] == entry["candidate_id"]
    assert prepared["expected_production_source_id"] == entry["candidate_id"]
    assert prepared["idempotency_key"].endswith(prepared["content_sha256"])
    serialized = json.dumps(prepared).casefold()
    assert '"approved"' not in serialized
    assert '"review_status"' not in serialized


def test_unresolved_practitioner_type_blocks_payload_compilation(
    tmp_path: Path,
) -> None:
    entry = _manifest_entry(
        build_acquisition_manifest(SOURCE_RUN), unresolved_source_type=True
    )
    receipt = _stage_receipt(tmp_path, entry, "Exact unofficial archive text.")

    with pytest.raises(AcquisitionStagingError) as caught:
        build_production_submission(entry, receipt, tmp_path)

    assert caught.value.code == "source_type_human_choice_required"


def test_manifest_content_slot_cannot_be_redirected_to_bootstrap_artifact(
    tmp_path: Path,
) -> None:
    entry = _manifest_entry(build_acquisition_manifest(SOURCE_RUN))
    receipt = _stage_receipt(tmp_path, entry, "Exact acquired source text.")
    tampered = json.loads(json.dumps(entry))
    tampered["exact_content"]["content_file"] = (
        "../bootstrap-001/evidence_cards/fallback.json"
    )
    changed_receipt = receipt.model_copy(
        update={"content_file": tampered["exact_content"]["content_file"]}
    )

    with pytest.raises(AcquisitionStagingError) as caught:
        build_production_submission(tampered, changed_receipt, tmp_path)

    assert caught.value.code == "unsafe_content_path"
