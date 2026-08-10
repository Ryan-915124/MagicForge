"""Offline validation tests for saved Tavily Source-acquisition exports."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from research.acquisition.staging import (
    AcquisitionStagingError,
    build_acquisition_manifest,
    write_acquisition_manifest,
)
from research.acquisition.tavily_results import (
    process_saved_tavily_exports,
    verify_staged_acquisition_results,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_RUN = REPOSITORY_ROOT / "research/runs/magicforge-corpus-run-001"
UNRESOLVED_ID = "282bfa90-a30b-5604-9dd5-a046cee0b2fe"


@pytest.fixture(autouse=True)
def _use_public_synthetic_source_run(synthetic_acquisition_locks):
    global SOURCE_RUN, UNRESOLVED_ID

    previous = (SOURCE_RUN, UNRESOLVED_ID)
    SOURCE_RUN = synthetic_acquisition_locks.root
    UNRESOLVED_ID = synthetic_acquisition_locks.unresolved_candidate_id
    try:
        yield
    finally:
        SOURCE_RUN, UNRESOLVED_ID = previous


def _selected_locator(entry: dict) -> dict:
    kind = (
        "publisher_or_repository_html"
        if entry["source_category"] == "academic"
        else "practitioner_web_page"
    )
    return next(
        locator
        for locator in entry["acquisition"]["content_locators"]
        if locator["kind"] == kind
    )


def _valid_content(entry: dict) -> str:
    title = entry["citation"]["title"]
    padding = "Research and performance context remains preserved. " * 130
    if entry["source_category"] == "academic":
        return (
            f"# {title}\n\n"
            f"DOI: {entry['citation']['doi']}\n\n"
            "## Abstract\nThe study addresses a defined research question.\n\n"
            "## Introduction\nPrior work establishes the context.\n\n"
            "## Methods\nThe paper reports its method.\n\n"
            "## Results\nThe paper reports its results.\n\n"
            "## References\nCited literature is listed.\n\n"
            f"{padding}"
        )
    return f"# {title}\n\n{padding}"


def _result(entry: dict) -> dict:
    locator = _selected_locator(entry)
    return {
        "url": locator["url"],
        "title": entry["citation"]["title"],
        "raw_content": _valid_content(entry),
        "images": [],
    }


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_rehashed_manifest(path: Path, manifest: dict) -> None:
    unhashed = dict(manifest)
    unhashed.pop("manifest_sha256", None)
    canonical = json.dumps(
        unhashed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    manifest["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    manifest = build_acquisition_manifest(SOURCE_RUN)
    manifest_path = tmp_path / "acquisition-manifest.json"
    write_acquisition_manifest(SOURCE_RUN, manifest_path)
    input_dir = tmp_path / "saved-exports"
    output_dir = tmp_path / "output"

    practitioner = sorted(
        (
            entry
            for entry in manifest["entries"]
            if entry["acquisition"]["status"] == "ready"
            and entry["source_category"] == "practitioner"
        ),
        key=lambda entry: entry["candidate_id"],
    )
    academic = sorted(
        (
            entry
            for entry in manifest["entries"]
            if entry["acquisition"]["status"] == "ready"
            and entry["source_category"] == "academic"
        ),
        key=lambda entry: entry["candidate_id"],
    )
    fallback = academic[-5:]
    basic = academic[:-5]

    _write_json(
        input_dir / "practitioner-basic.json",
        {
            "request_id": "test-practitioner-basic-request",
            "response_time": 0.1,
            "results": [_result(entry) for entry in practitioner],
            "failed_results": [],
        },
    )
    _write_json(
        input_dir / "academic-basic.json",
        {
            "request_id": "test-academic-basic-request",
            "response_time": 0.2,
            "results": [_result(entry) for entry in basic],
            "failed_results": [
                {"url": _selected_locator(entry)["url"], "error": "fetch failed"}
                for entry in fallback
            ],
        },
    )
    _write_json(
        input_dir / "academic-advanced-fallback.json",
        {
            "request_id": "test-academic-advanced-request",
            "response_time": 0.3,
            "results": [_result(entry) for entry in fallback],
            "failed_results": [],
        },
    )
    # This fourth file must be ignored by design.
    _write_json(
        input_dir / "unresolved-pdf-tavily.json",
        {
            "request_id": "must-not-be-processed",
            "results": [
                {
                    "url": "https://example.test/unresolved.pdf",
                    "title": "Unresolved PDF",
                    "raw_content": "FORBIDDEN_UNRESOLVED_PDF_SENTINEL",
                }
            ],
            "failed_results": [],
        },
    )
    return manifest_path, input_dir, output_dir, manifest


def _read_export(input_dir: Path, filename: str) -> dict:
    return json.loads((input_dir / filename).read_text(encoding="utf-8"))


def test_saved_exports_stage_exact_30_source_bundle(tmp_path: Path) -> None:
    manifest_path, input_dir, output_dir, manifest = _fixture(tmp_path)

    results = process_saved_tavily_exports(manifest_path, input_dir, output_dir)

    assert results["schema_version"] == "source-acquisition-results-0.2"
    assert results["summary"] == {
        "selected_results": 30,
        "validated_results": 30,
        "content_files": 30,
        "receipts": 30,
        "source_payloads": 29,
        "receipt_only": 1,
        "basic_results": 25,
        "advanced_fallback_results": 5,
    }
    assert len(list((output_dir / "content").glob("*.txt"))) == 30
    assert len(list((output_dir / "receipts").glob("*.json"))) == 30
    assert len(list((output_dir / "source_payloads").glob("*.json"))) == 29
    assert not (output_dir / "source_payloads" / f"{UNRESOLVED_ID}.json").exists()
    assert (output_dir / "receipts" / f"{UNRESOLVED_ID}.json").is_file()
    assert (output_dir / "acquisition-results.json").is_file()
    assert (output_dir / "run-report.md").is_file()

    for receipt_path in (output_dir / "receipts").glob("*.json"):
        receipt = json.loads(receipt_path.read_text())
        assert receipt["content_access"] == "web_extract"
        assert receipt["returned_url"] == receipt["source_locator"]
        assert datetime.fromisoformat(receipt["provider_response_saved_at"]).tzinfo

    for item in results["items"]:
        receipt_bytes = (output_dir / item["receipt_file"]).read_bytes()
        assert item["receipt_sha256"] == hashlib.sha256(receipt_bytes).hexdigest()
        if item["payload_status"] == "prepared":
            payload_bytes = (output_dir / item["payload_file"]).read_bytes()
            assert item["payload_sha256"] == hashlib.sha256(payload_bytes).hexdigest()
        else:
            assert item["payload_file"] is None
            assert item["payload_sha256"] is None

    assert verify_staged_acquisition_results(output_dir) == {
        "items_verified": 30,
        "receipt_hashes_verified": 30,
        "payload_hashes_verified": 29,
        "receipt_only_verified": 1,
    }

    by_id = {entry["candidate_id"]: entry for entry in manifest["entries"]}
    sample = next(
        item for item in results["items"] if item["payload_status"] == "prepared"
    )
    prepared = json.loads((output_dir / sample["payload_file"]).read_text())
    payload = prepared["payload"]
    assert payload["content"] == _valid_content(by_id[sample["candidate_id"]])
    assert payload["content_access"] == "web_extract"
    assert payload["requested_extraction_permission"] == "none"
    assert payload["requested_storage_permission"] == "none"
    assert payload["sensitive_information_level"] == "controlled"
    assert payload["citation_verification"] == []
    assert payload["access"]["redistribution_allowed"] is False

    serialized = json.dumps(results).casefold()
    assert '"approval"' not in serialized
    assert "forbidden_unresolved_pdf_sentinel" not in serialized
    for path in (
        list((output_dir / "content").glob("*.txt"))
        + list((output_dir / "receipts").glob("*.json"))
        + list((output_dir / "source_payloads").glob("*.json"))
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_exact_tavily_raw_content_bytes_are_preserved(tmp_path: Path) -> None:
    manifest_path, input_dir, output_dir, _manifest = _fixture(tmp_path)
    export = _read_export(input_dir, "practitioner-basic.json")
    source = export["results"][0]
    raw = source["raw_content"] + "\nTRAILING-WHITESPACE  \n"
    source["raw_content"] = raw
    _write_json(input_dir / "practitioner-basic.json", export)
    saved_epoch = 1_700_000_000
    os.utime(
        input_dir / "practitioner-basic.json",
        (saved_epoch, saved_epoch),
    )

    results = process_saved_tavily_exports(manifest_path, input_dir, output_dir)
    item = next(item for item in results["items"] if item["result_url"] == source["url"])
    stored = (output_dir / item["content_file"]).read_bytes()

    assert stored == raw.encode("utf-8")
    assert item["content_sha256"] == hashlib.sha256(stored).hexdigest()
    receipt = json.loads((output_dir / item["receipt_file"]).read_text())
    assert receipt["provider_request_id"] == "test-practitioner-basic-request"
    assert receipt["extract_depth"] == "basic"
    assert receipt["returned_url"] == source["url"]
    saved_at = datetime.fromisoformat(receipt["provider_response_saved_at"])
    assert saved_at.tzinfo is not None
    assert saved_at == datetime.fromtimestamp(saved_epoch, tz=UTC)
    assert receipt["validation"]["passed"] is True


def test_relative_output_root_is_resolved_consistently(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path, input_dir, _output_dir, _manifest = _fixture(tmp_path)
    monkeypatch.chdir(tmp_path)

    results = process_saved_tavily_exports(
        manifest_path, input_dir, Path("relative-output")
    )

    assert results["summary"]["source_payloads"] == 29
    assert (tmp_path / "relative-output/acquisition-results.json").is_file()


def test_unknown_result_url_fails_before_any_artifact_write(tmp_path: Path) -> None:
    manifest_path, input_dir, output_dir, _manifest = _fixture(tmp_path)
    export = _read_export(input_dir, "practitioner-basic.json")
    export["results"][0]["url"] = "https://example.test/not-in-manifest"
    _write_json(input_dir / "practitioner-basic.json", export)

    with pytest.raises(AcquisitionStagingError) as caught:
        process_saved_tavily_exports(manifest_path, input_dir, output_dir)

    assert caught.value.code == "practitioner_url_set_mismatch"
    assert not output_dir.exists()


def test_short_or_titleless_content_fails_closed(tmp_path: Path) -> None:
    manifest_path, input_dir, output_dir, _manifest = _fixture(tmp_path)
    export = _read_export(input_dir, "practitioner-basic.json")
    export["results"][0]["raw_content"] = "short"
    _write_json(input_dir / "practitioner-basic.json", export)

    with pytest.raises(AcquisitionStagingError) as short:
        process_saved_tavily_exports(manifest_path, input_dir, output_dir)
    assert short.value.code == "exact_content_validation_failed"
    assert "minimum_length" in str(short.value)
    assert not output_dir.exists()

    export["results"][0]["raw_content"] = "generic performance prose " * 300
    # Keep the correct provider title: it must not rescue a body that lacks the
    # candidate title tokens.
    _write_json(input_dir / "practitioner-basic.json", export)
    with pytest.raises(AcquisitionStagingError) as titleless:
        process_saved_tavily_exports(manifest_path, input_dir, output_dir)
    assert "title_token_coverage" in str(titleless.value)
    assert not output_dir.exists()


def test_academic_doi_and_structure_are_required(tmp_path: Path) -> None:
    manifest_path, input_dir, output_dir, manifest = _fixture(tmp_path)
    by_url = {
        _selected_locator(entry)["url"]: entry
        for entry in manifest["entries"]
        if entry["acquisition"]["status"] == "ready"
        and entry["source_category"] == "academic"
    }
    export = _read_export(input_dir, "academic-basic.json")
    target = next(
        result
        for result in export["results"]
        if by_url[result["url"]]["citation"]["doi"].casefold()
        in result["url"].casefold()
    )
    entry = by_url[target["url"]]
    target["raw_content"] = _valid_content(entry).replace(
        entry["citation"]["doi"], "identifier withheld"
    )
    _write_json(input_dir / "academic-basic.json", export)

    with pytest.raises(AcquisitionStagingError) as doi:
        process_saved_tavily_exports(manifest_path, input_dir, output_dir)
    assert "academic_doi" in str(doi.value)
    assert str(doi.value).count("academic_doi") == 1
    assert not output_dir.exists()

    target["raw_content"] = (
        f"# {entry['citation']['title']}\nDOI {entry['citation']['doi']}\n"
        + "unstructured article body " * 300
    )
    _write_json(input_dir / "academic-basic.json", export)
    with pytest.raises(AcquisitionStagingError) as structure:
        process_saved_tavily_exports(manifest_path, input_dir, output_dir)
    assert "academic_structure" in str(structure.value)
    assert not output_dir.exists()


def test_advanced_export_can_only_replace_basic_failures(tmp_path: Path) -> None:
    manifest_path, input_dir, output_dir, _manifest = _fixture(tmp_path)
    advanced = _read_export(input_dir, "academic-advanced-fallback.json")
    advanced["results"].pop()
    _write_json(input_dir / "academic-advanced-fallback.json", advanced)

    with pytest.raises(AcquisitionStagingError) as caught:
        process_saved_tavily_exports(manifest_path, input_dir, output_dir)

    assert caught.value.code == "advanced_fallback_url_set_mismatch"
    assert not output_dir.exists()


@pytest.mark.parametrize(
    "mutation, expected_code",
    [
        ("run_id", "manifest_run_identity_mismatch"),
        ("source_run_id", "manifest_run_identity_mismatch"),
        ("constraints", "manifest_constraints_lock_mismatch"),
        ("input_lock", "manifest_input_lock_mismatch"),
        ("duplicate_candidate", "manifest_duplicate_candidate_id"),
    ],
)
def test_recomputed_hash_cannot_bypass_manifest_locks(
    tmp_path: Path, mutation: str, expected_code: str
) -> None:
    manifest_path, input_dir, output_dir, _manifest = _fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "run_id":
        manifest["run_id"] = "attacker-rehashed-run"
    elif mutation == "source_run_id":
        manifest["source_run_id"] = "attacker-source-run"
    elif mutation == "constraints":
        manifest["constraints"]["api_submission"] = True
    elif mutation == "input_lock":
        manifest["inputs"][0]["sha256"] = "0" * 64
    else:
        manifest["entries"][0]["candidate_id"] = manifest["entries"][1][
            "candidate_id"
        ]
    _write_rehashed_manifest(manifest_path, manifest)

    with pytest.raises(AcquisitionStagingError) as caught:
        process_saved_tavily_exports(manifest_path, input_dir, output_dir)

    assert caught.value.code == expected_code
    assert not output_dir.exists()


@pytest.mark.parametrize(
    "container, field",
    [
        ("exact_content", "content_file"),
        ("exact_content", "receipt_file"),
        ("production", "payload_file"),
    ],
)
def test_rehashed_manifest_traversal_paths_are_rejected_before_writes(
    tmp_path: Path, container: str, field: str
) -> None:
    manifest_path, input_dir, output_dir, _manifest = _fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = next(
        entry
        for entry in manifest["entries"]
        if entry["acquisition"]["status"] == "ready"
        and entry["classification"]["source_type"] is not None
    )
    target[container][field] = "../../outside-acquisition.txt"
    _write_rehashed_manifest(manifest_path, manifest)

    with pytest.raises(AcquisitionStagingError) as caught:
        process_saved_tavily_exports(manifest_path, input_dir, output_dir)

    assert caught.value.code == "unsafe_output_path"
    assert not output_dir.exists()
    assert not (tmp_path / "outside-acquisition.txt").exists()


@pytest.mark.parametrize("tamper", ["metadata", "content"])
def test_payload_tampering_without_item_hash_update_is_detected(
    tmp_path: Path, tamper: str
) -> None:
    manifest_path, input_dir, output_dir, _manifest = _fixture(tmp_path)
    results = process_saved_tavily_exports(manifest_path, input_dir, output_dir)
    item = next(
        value for value in results["items"] if value["payload_status"] == "prepared"
    )
    payload_path = output_dir / item["payload_file"]
    wrapper = json.loads(payload_path.read_text(encoding="utf-8"))
    if tamper == "metadata":
        wrapper["payload"]["citation"]["title"] = "Tampered citation title"
    else:
        wrapper["payload"]["content"] += "\nTampered payload-only content."
    _write_json(payload_path, wrapper)

    with pytest.raises(AcquisitionStagingError) as caught:
        verify_staged_acquisition_results(output_dir)

    assert caught.value.code == "payload_file_hash_mismatch"
