"""Validate saved Tavily exact-URL exports and stage governed Source payloads.

The processor is deliberately offline: it reads exactly three previously saved
Tavily JSON exports and an acquisition manifest.  It never imports a network
client, submits an API request, creates a review decision, or writes a database.
All 30 selected results are validated before any artifact is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from research.acquisition.staging import (
    ACQUISITION_RUN_ID,
    EXPECTED_CANDIDATE_IDS,
    EXPECTED_INPUT_LOCK,
    EXPECTED_MANIFEST_CONSTRAINTS,
    MANIFEST_SCHEMA_VERSION,
    SOURCE_RUN_ID,
    AcquisitionStagingError,
    ExactContentReceipt,
    build_production_submission_from_content,
)
from research.models import ContentAccess
from research.review.workflow_models import SourceVersionCommand


RESULTS_SCHEMA_VERSION = "source-acquisition-results-0.2"
MINIMUM_CONTENT_CHARACTERS = 5_000
MINIMUM_TITLE_TOKEN_COVERAGE = 0.75
UNRESOLVED_SOURCE_TYPE_CANDIDATE_ID = "282bfa90-a30b-5604-9dd5-a046cee0b2fe"

_EXPORT_SPECS: tuple[tuple[str, Literal["basic", "advanced"]], ...] = (
    ("practitioner-basic.json", "basic"),
    ("academic-basic.json", "basic"),
    ("academic-advanced-fallback.json", "advanced"),
)

_TITLE_STOP_WORDS = {
    "and",
    "are",
    "for",
    "from",
    "how",
    "into",
    "the",
    "their",
    "this",
    "using",
    "with",
}

_STRUCTURE_PATTERNS = {
    "abstract": re.compile(r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?abstract\b"),
    "introduction": re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:\d+[.\s-]+)?introduction\b"
    ),
    "method": re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:\d+[.\s-]+)?"
        r"(?:methods?|methodology|survey\s+methodology|"
        r"materials\s+and\s+methods?|the\s+method\b)"
    ),
    "results": re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:\d+[.\s-]+)?results?\b"
    ),
    "discussion": re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:\d+[.\s-]+)?discussion\b"
    ),
    "references": re.compile(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:references|bibliography)\b"
    ),
}


@dataclass(frozen=True)
class _ExtractedResult:
    entry: dict[str, Any]
    raw_content: str
    result_title: str
    result_url: str
    request_id: str
    extract_depth: Literal["basic", "advanced"]
    provider_response_saved_at: datetime
    validation: dict[str, Any]


def process_saved_tavily_exports(
    manifest_path: Path,
    input_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Validate all saved exports, then atomically stage the 30-source bundle."""

    manifest = _read_json_object(Path(manifest_path), "invalid_manifest_json")
    _validate_manifest(manifest)
    entries = {
        str(entry["candidate_id"]): entry for entry in manifest.get("entries", [])
    }
    expected = _expected_locator_sets(entries)

    exports: dict[str, dict[str, Any]] = {}
    export_saved_at: dict[str, datetime] = {}
    input_records: list[dict[str, Any]] = []
    for filename, depth in _EXPORT_SPECS:
        path = Path(input_dir) / filename
        export = _read_json_object(path, "invalid_tavily_export")
        _validate_export_envelope(export, filename)
        exports[filename] = export
        saved_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        export_saved_at[filename] = saved_at
        input_records.append(
            {
                "file": filename,
                "sha256": _sha256_bytes(path.read_bytes()),
                "provider": "tavily",
                "provider_request_id": export["request_id"],
                "extract_depth": depth,
                "result_count": len(export["results"]),
                "failed_count": len(export["failed_results"]),
                "response_time": export.get("response_time"),
                "provider_response_saved_at": saved_at.isoformat(),
            }
        )

    selected = _select_exact_results(exports, export_saved_at, entries, expected)
    if len(selected) != 30:
        raise AcquisitionStagingError(
            "validated_result_count_mismatch",
            "The saved Tavily exports must yield exactly 30 selected results.",
        )

    # Build every in-memory artifact first.  File writes begin only after all
    # result-level checks have passed.
    artifacts: list[dict[str, Any]] = []
    for result in sorted(selected, key=lambda value: value.entry["candidate_id"]):
        raw = result.raw_content.encode("utf-8")
        content_hash = _sha256_bytes(raw)
        candidate_id = result.entry["candidate_id"]
        locator = _locator_for_url(result.entry, result.result_url)
        receipt = ExactContentReceipt(
            candidate_id=candidate_id,
            content_file=result.entry["exact_content"]["content_file"],
            # Tavily returned extracted HTML/Markdown, not an immutable PDF
            # byte stream.  This remains ``web_extract`` for academic pages too.
            content_access=ContentAccess.WEB_EXTRACT,
            source_locator=result.result_url,
            acquisition_method=(
                f"tavily_exact_url_extract:{result.extract_depth}"
            ),
            content_sha256=content_hash,
            content_bytes=len(raw),
            access={
                "access_method": (
                    f"Tavily exact-URL extract ({result.extract_depth})"
                ),
                "license_name": locator.get("license_name"),
                "rights_uri": None,
                "permission_notes": (
                    "Exact-URL acquisition staged for governed Source review; "
                    f"Tavily request {result.request_id}."
                ),
                "redistribution_allowed": False,
            },
            provider="tavily",
            provider_request_id=result.request_id,
            extract_depth=result.extract_depth,
            provider_result_title=result.result_title,
            provider_response_saved_at=result.provider_response_saved_at,
            returned_url=result.result_url,
            validation=result.validation,
        )
        artifacts.append(
            {
                "result": result,
                "raw": raw,
                "content_hash": content_hash,
                "receipt": receipt,
            }
        )

    output_dir = Path(output_dir).resolve()
    _preflight_output_paths(output_dir, artifacts)

    # Serialize and hash the complete bundle before the first write.  Result
    # metadata therefore commits to the exact receipt and payload-wrapper bytes
    # that will be persisted below.
    result_items: list[dict[str, Any]] = []
    for artifact in artifacts:
        result: _ExtractedResult = artifact["result"]
        entry = result.entry
        candidate_id = entry["candidate_id"]
        content_path = _fixed_output_path(
            output_dir,
            entry["exact_content"]["content_file"],
            f"content/{candidate_id}.txt",
        )
        receipt_path = _fixed_output_path(
            output_dir,
            entry["exact_content"]["receipt_file"],
            f"receipts/{candidate_id}.json",
        )
        expected_payload_path = _fixed_output_path(
            output_dir,
            entry["production"]["payload_file"],
            f"source_payloads/{candidate_id}.json",
        )
        receipt_bytes = _json_bytes(artifact["receipt"].model_dump(mode="json"))

        payload_path: Path | None = None
        payload_bytes: bytes | None = None
        payload_status = "prepared"
        payload_blocker: str | None = None
        if entry["classification"]["source_type"] is None:
            if candidate_id != UNRESOLVED_SOURCE_TYPE_CANDIDATE_ID:
                raise AcquisitionStagingError(
                    "unexpected_unresolved_source_type",
                    "Only the locked unofficial interview archive may remain untyped.",
                )
            payload_status = "receipt_only_source_type_unresolved"
            payload_blocker = "source_type_human_choice_required"
        else:
            prepared = build_production_submission_from_content(
                entry, artifact["receipt"], artifact["raw"]
            )
            payload_path = expected_payload_path
            payload_bytes = _json_bytes(prepared)

        artifact.update(
            {
                "content_path": content_path,
                "receipt_path": receipt_path,
                "receipt_bytes": receipt_bytes,
                "payload_path": payload_path,
                "payload_bytes": payload_bytes,
            }
        )

        result_items.append(
            {
                "candidate_id": candidate_id,
                "source_category": entry["source_category"],
                "provider": "tavily",
                "provider_request_id": result.request_id,
                "provider_response_saved_at": (
                    result.provider_response_saved_at.isoformat()
                ),
                "extract_depth": result.extract_depth,
                "result_url": result.result_url,
                "result_title": result.result_title,
                "content_sha256": artifact["content_hash"],
                "content_bytes": len(artifact["raw"]),
                "validation": result.validation,
                "content_file": content_path.relative_to(output_dir).as_posix(),
                "receipt_file": receipt_path.relative_to(output_dir).as_posix(),
                "receipt_sha256": _sha256_bytes(receipt_bytes),
                "payload_status": payload_status,
                "payload_blocker": payload_blocker,
                "payload_file": (
                    payload_path.relative_to(output_dir).as_posix()
                    if payload_path is not None
                    else None
                ),
                "payload_sha256": (
                    _sha256_bytes(payload_bytes)
                    if payload_bytes is not None
                    else None
                ),
            }
        )

    results_document: dict[str, Any] = {
        "schema_version": RESULTS_SCHEMA_VERSION,
        "run_id": ACQUISITION_RUN_ID,
        "source_manifest": Path(manifest_path).name,
        "source_manifest_sha256": manifest["manifest_sha256"],
        "constraints": {
            "saved_exports_only": True,
            "network_access": False,
            "api_submission": False,
            "database_write": False,
            "review_decision_created": False,
            "unresolved_pdf_export_processed": False,
            "requested_extraction_permission": "none",
            "requested_storage_permission": "none",
            "sensitive_information_level": "controlled",
            "citation_verification": [],
            "redistribution_allowed": False,
        },
        "inputs": input_records,
        "summary": {
            "selected_results": len(result_items),
            "validated_results": sum(
                item["validation"]["passed"] for item in result_items
            ),
            "content_files": len(result_items),
            "receipts": len(result_items),
            "source_payloads": sum(
                item["payload_status"] == "prepared" for item in result_items
            ),
            "receipt_only": sum(
                item["payload_status"] != "prepared" for item in result_items
            ),
            "basic_results": sum(
                item["extract_depth"] == "basic" for item in result_items
            ),
            "advanced_fallback_results": sum(
                item["extract_depth"] == "advanced" for item in result_items
            ),
        },
        "items": result_items,
    }
    results_document["results_sha256"] = _canonical_json_hash(results_document)
    results_bytes = _json_bytes(results_document)
    report_bytes = _run_report(results_document).encode("utf-8")

    for directory in ("content", "receipts", "source_payloads"):
        (output_dir / directory).mkdir(parents=True, exist_ok=True)
    for artifact in artifacts:
        _atomic_write_bytes(artifact["content_path"], artifact["raw"])
        _atomic_write_bytes(artifact["receipt_path"], artifact["receipt_bytes"])
        if artifact["payload_path"] is not None:
            _atomic_write_bytes(artifact["payload_path"], artifact["payload_bytes"])
    _atomic_write_bytes(
        _fixed_output_path(
            output_dir, "acquisition-results.json", "acquisition-results.json"
        ),
        results_bytes,
    )
    _atomic_write_bytes(
        _fixed_output_path(output_dir, "run-report.md", "run-report.md"),
        report_bytes,
    )
    verify_staged_acquisition_results(output_dir, results_document)
    return results_document


def validate_exact_result(
    entry: dict[str, Any],
    *,
    raw_content: str,
    result_title: str,
    result_url: str,
) -> dict[str, Any]:
    """Return an auditable quality record or fail closed."""

    character_count = len(raw_content)
    non_empty = bool(raw_content.strip())
    length_passed = character_count >= MINIMUM_CONTENT_CHARACTERS

    expected_tokens = _title_tokens(str(entry["citation"]["title"]))
    # The provider-supplied title is diagnostic only.  The stored source body
    # must independently contain enough of the candidate title.
    observed_tokens = _title_tokens(raw_content)
    matched_tokens = sorted(expected_tokens & observed_tokens)
    missing_tokens = sorted(expected_tokens - observed_tokens)
    coverage = len(matched_tokens) / len(expected_tokens) if expected_tokens else 0.0
    title_passed = bool(expected_tokens) and coverage >= MINIMUM_TITLE_TOKEN_COVERAGE

    academic = entry["source_category"] == "academic"
    doi = str(entry["citation"].get("doi") or "").strip().casefold()
    doi_in_url = bool(academic and doi and doi in result_url.casefold())
    doi_in_content = bool(academic and doi and doi in raw_content.casefold())
    # URL presence is retained for diagnostics, but cannot substitute for a DOI
    # actually present in the extracted academic body.
    doi_passed: bool | None = doi_in_content if academic else None

    # Some publisher Markdown inserts an empty anchor between the heading mark
    # and label (for example ``## [](url)Abstract``).  Remove link syntax only
    # for structure detection; the stored exact text remains byte-for-byte.
    structure_probe = re.sub(r"\[[^\]]*\]\([^)]+\)", "", raw_content)
    structure_markers = sorted(
        name
        for name, pattern in _STRUCTURE_PATTERNS.items()
        if pattern.search(structure_probe)
    )
    required_structure = {"abstract", "references"}
    analytic_structure = {"introduction", "method", "results", "discussion"}
    structure_passed: bool | None = (
        required_structure.issubset(structure_markers)
        and len(analytic_structure.intersection(structure_markers)) >= 2
        if academic
        else None
    )

    passed = bool(
        non_empty
        and length_passed
        and title_passed
        and (not academic or (doi_passed and structure_passed))
    )
    validation = {
        "passed": passed,
        "non_empty": non_empty,
        "character_count": character_count,
        "minimum_character_count": MINIMUM_CONTENT_CHARACTERS,
        "minimum_length_passed": length_passed,
        "title_token_coverage": round(coverage, 6),
        "minimum_title_token_coverage": MINIMUM_TITLE_TOKEN_COVERAGE,
        "title_tokens_expected": len(expected_tokens),
        "title_tokens_matched": len(matched_tokens),
        "title_tokens_missing": missing_tokens,
        "title_token_check_passed": title_passed,
        "academic_doi": doi if academic else None,
        "academic_doi_in_url": doi_in_url if academic else None,
        "academic_doi_in_content": doi_in_content if academic else None,
        "academic_doi_check_passed": doi_passed,
        "academic_structure_markers": structure_markers if academic else [],
        "academic_structure_check_passed": structure_passed,
    }
    if not passed:
        failed_checks: list[str] = []
        if not non_empty:
            failed_checks.append("non_empty")
        if not length_passed:
            failed_checks.append("minimum_length")
        if not title_passed:
            failed_checks.append("title_token_coverage")
        if academic and not doi_passed:
            failed_checks.append("academic_doi")
        if academic and not structure_passed:
            failed_checks.append("academic_structure")
        raise AcquisitionStagingError(
            "exact_content_validation_failed",
            f"Candidate {entry['candidate_id']} failed: {', '.join(failed_checks)}.",
        )
    return validation


def verify_staged_acquisition_results(
    output_dir: Path,
    results_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-read the staged bundle and verify every committed byte hash."""

    output_dir = Path(output_dir).resolve()
    results_path = _fixed_output_path(
        output_dir, "acquisition-results.json", "acquisition-results.json"
    )
    if not results_path.is_file():
        raise AcquisitionStagingError(
            "acquisition_results_missing", "Acquisition results file is missing."
        )
    on_disk_results = _read_json_object(results_path, "invalid_acquisition_results")
    if results_document is not None:
        expected_results_bytes = _json_bytes(results_document)
        if _sha256_bytes(results_path.read_bytes()) != _sha256_bytes(
            expected_results_bytes
        ):
            raise AcquisitionStagingError(
                "acquisition_results_file_hash_mismatch",
                "Written acquisition-results bytes differ from the in-memory bundle.",
            )
        if on_disk_results != results_document:
            raise AcquisitionStagingError(
                "acquisition_results_document_mismatch",
                "Written acquisition results differ from the in-memory document.",
            )
    results = results_document or on_disk_results
    if results.get("schema_version") != RESULTS_SCHEMA_VERSION:
        raise AcquisitionStagingError(
            "acquisition_results_schema_mismatch",
            "Unsupported acquisition results schema.",
        )
    unhashed = dict(results)
    recorded_results_hash = str(unhashed.pop("results_sha256", ""))
    if not recorded_results_hash or recorded_results_hash != _canonical_json_hash(
        unhashed
    ):
        raise AcquisitionStagingError(
            "acquisition_results_hash_mismatch",
            "Acquisition results self-hash is invalid.",
        )

    items = results.get("items")
    if not isinstance(items, list) or len(items) != 30:
        raise AcquisitionStagingError(
            "acquisition_result_item_count_mismatch",
            "Acquisition results must contain exactly 30 items.",
        )
    candidate_ids = [str(item.get("candidate_id") or "") for item in items]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise AcquisitionStagingError(
            "acquisition_result_duplicate_candidate",
            "Acquisition result candidate IDs must be unique.",
        )

    prepared_count = 0
    receipt_only_count = 0
    for item in items:
        candidate_id = str(item.get("candidate_id") or "")
        content_path = _fixed_output_path(
            output_dir,
            item.get("content_file"),
            f"content/{candidate_id}.txt",
        )
        receipt_path = _fixed_output_path(
            output_dir,
            item.get("receipt_file"),
            f"receipts/{candidate_id}.json",
        )
        if not content_path.is_file() or not receipt_path.is_file():
            raise AcquisitionStagingError(
                "staged_file_missing", f"Staged content/receipt is missing for {candidate_id}."
            )
        content_bytes = content_path.read_bytes()
        receipt_bytes = receipt_path.read_bytes()
        if _sha256_bytes(content_bytes) != item.get("content_sha256"):
            raise AcquisitionStagingError(
                "content_file_hash_mismatch",
                f"Staged content hash mismatch for {candidate_id}.",
            )
        if len(content_bytes) != item.get("content_bytes"):
            raise AcquisitionStagingError(
                "content_file_length_mismatch",
                f"Staged content byte length mismatch for {candidate_id}.",
            )
        if _sha256_bytes(receipt_bytes) != item.get("receipt_sha256"):
            raise AcquisitionStagingError(
                "receipt_file_hash_mismatch",
                f"Staged receipt hash mismatch for {candidate_id}.",
            )
        try:
            receipt = ExactContentReceipt.model_validate_json(receipt_bytes)
        except ValueError as exc:
            raise AcquisitionStagingError(
                "invalid_staged_receipt", f"Staged receipt is invalid for {candidate_id}."
            ) from exc
        if (
            str(receipt.candidate_id) != candidate_id
            or receipt.content_sha256 != item.get("content_sha256")
            or receipt.content_bytes != len(content_bytes)
        ):
            raise AcquisitionStagingError(
                "receipt_content_commitment_mismatch",
                f"Receipt does not commit to content for {candidate_id}.",
            )

        if item.get("payload_status") == "prepared":
            prepared_count += 1
            payload_path = _fixed_output_path(
                output_dir,
                item.get("payload_file"),
                f"source_payloads/{candidate_id}.json",
            )
            if not payload_path.is_file():
                raise AcquisitionStagingError(
                    "payload_file_missing", f"Payload file is missing for {candidate_id}."
                )
            payload_bytes = payload_path.read_bytes()
            if _sha256_bytes(payload_bytes) != item.get("payload_sha256"):
                raise AcquisitionStagingError(
                    "payload_file_hash_mismatch",
                    f"Payload wrapper hash mismatch for {candidate_id}.",
                )
            try:
                wrapper = json.loads(payload_bytes.decode("utf-8"))
                command = SourceVersionCommand.model_validate(wrapper["payload"])
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
                raise AcquisitionStagingError(
                    "invalid_staged_payload",
                    f"Payload wrapper is invalid for {candidate_id}.",
                ) from exc
            command_content_hash = _sha256_bytes(command.content.encode("utf-8"))
            if (
                wrapper.get("candidate_id") != candidate_id
                or wrapper.get("expected_production_source_id") != candidate_id
                or wrapper.get("content_sha256") != item.get("content_sha256")
                or command_content_hash != item.get("content_sha256")
            ):
                raise AcquisitionStagingError(
                    "payload_content_commitment_mismatch",
                    f"Payload does not commit to staged content for {candidate_id}.",
                )
        else:
            receipt_only_count += 1
            if (
                candidate_id != UNRESOLVED_SOURCE_TYPE_CANDIDATE_ID
                or item.get("payload_file") is not None
                or item.get("payload_sha256") is not None
            ):
                raise AcquisitionStagingError(
                    "invalid_receipt_only_result",
                    "Only the locked unresolved Source may omit its payload commitment.",
                )
            unexpected_payload = _fixed_output_path(
                output_dir,
                f"source_payloads/{candidate_id}.json",
                f"source_payloads/{candidate_id}.json",
            )
            if unexpected_payload.exists():
                raise AcquisitionStagingError(
                    "unexpected_receipt_only_payload",
                    "Receipt-only Source unexpectedly has a payload file.",
                )

    if prepared_count != 29 or receipt_only_count != 1:
        raise AcquisitionStagingError(
            "payload_commitment_count_mismatch",
            "Expected 29 payload commitments and one receipt-only item.",
        )
    return {
        "items_verified": len(items),
        "receipt_hashes_verified": len(items),
        "payload_hashes_verified": prepared_count,
        "receipt_only_verified": receipt_only_count,
    }


def _expected_locator_sets(
    entries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, str]]:
    practitioner: dict[str, str] = {}
    academic: dict[str, str] = {}
    for candidate_id, entry in entries.items():
        if entry["acquisition"]["status"] != "ready":
            continue
        required_kind = (
            "publisher_or_repository_html"
            if entry["source_category"] == "academic"
            else "practitioner_web_page"
        )
        matches = [
            locator
            for locator in entry["acquisition"]["content_locators"]
            if locator["kind"] == required_kind
        ]
        if len(matches) != 1:
            raise AcquisitionStagingError(
                "manifest_primary_locator_invalid",
                f"Candidate {candidate_id} needs exactly one {required_kind} locator.",
            )
        canonical = _canonical_url(matches[0]["url"])
        target = academic if entry["source_category"] == "academic" else practitioner
        if canonical in target:
            raise AcquisitionStagingError(
                "manifest_locator_collision", "Two candidates share one content locator."
            )
        target[canonical] = candidate_id
    if len(practitioner) != 12 or len(academic) != 18:
        raise AcquisitionStagingError(
            "manifest_ready_set_mismatch",
            "Manifest must expose 12 practitioner and 18 academic ready locators.",
        )
    return {"practitioner": practitioner, "academic": academic}


def _select_exact_results(
    exports: dict[str, dict[str, Any]],
    export_saved_at: dict[str, datetime],
    entries: dict[str, dict[str, Any]],
    expected: dict[str, dict[str, str]],
) -> list[_ExtractedResult]:
    practitioner = exports["practitioner-basic.json"]
    academic_basic = exports["academic-basic.json"]
    academic_advanced = exports["academic-advanced-fallback.json"]

    practitioner_urls = _result_url_map(practitioner["results"], "practitioner-basic")
    practitioner_failed = _failed_url_set(
        practitioner["failed_results"], "practitioner-basic"
    )
    if practitioner_failed or set(practitioner_urls) != set(expected["practitioner"]):
        raise AcquisitionStagingError(
            "practitioner_url_set_mismatch",
            "Practitioner basic results must exactly match all 12 manifest locators.",
        )

    academic_success = _result_url_map(academic_basic["results"], "academic-basic")
    academic_failed = _failed_url_set(
        academic_basic["failed_results"], "academic-basic"
    )
    if set(academic_success).intersection(academic_failed):
        raise AcquisitionStagingError(
            "academic_basic_result_conflict",
            "An academic URL cannot be both successful and failed in one export.",
        )
    if set(academic_success).union(academic_failed) != set(expected["academic"]):
        raise AcquisitionStagingError(
            "academic_basic_url_set_mismatch",
            "Academic basic results/failures must cover exactly 18 manifest locators.",
        )
    advanced_success = _result_url_map(
        academic_advanced["results"], "academic-advanced-fallback"
    )
    advanced_failed = _failed_url_set(
        academic_advanced["failed_results"], "academic-advanced-fallback"
    )
    if advanced_failed or set(advanced_success) != academic_failed:
        raise AcquisitionStagingError(
            "advanced_fallback_url_set_mismatch",
            "Advanced results must exactly replace the academic basic failures.",
        )

    chosen: list[
        tuple[
            dict[str, Any],
            str,
            Literal["basic", "advanced"],
            str,
            datetime,
        ]
    ] = []
    for url, result in practitioner_urls.items():
        chosen.append(
            (
                result,
                practitioner["request_id"],
                "basic",
                expected["practitioner"][url],
                export_saved_at["practitioner-basic.json"],
            )
        )
    for url, result in academic_success.items():
        chosen.append(
            (
                result,
                academic_basic["request_id"],
                "basic",
                expected["academic"][url],
                export_saved_at["academic-basic.json"],
            )
        )
    for url, result in advanced_success.items():
        chosen.append(
            (
                result,
                academic_advanced["request_id"],
                "advanced",
                expected["academic"][url],
                export_saved_at["academic-advanced-fallback.json"],
            )
        )

    selected: list[_ExtractedResult] = []
    for result, request_id, depth, candidate_id, saved_at in chosen:
        raw_content = result.get("raw_content")
        result_title = result.get("title")
        result_url = result.get("url")
        if not isinstance(raw_content, str) or not isinstance(result_title, str):
            raise AcquisitionStagingError(
                "invalid_tavily_result_shape",
                "Every Tavily result requires string title and raw_content fields.",
            )
        if not isinstance(result_url, str):
            raise AcquisitionStagingError(
                "invalid_tavily_result_shape", "Every Tavily result requires a URL."
            )
        entry = entries[candidate_id]
        validation = validate_exact_result(
            entry,
            raw_content=raw_content,
            result_title=result_title,
            result_url=result_url,
        )
        selected.append(
            _ExtractedResult(
                entry=entry,
                raw_content=raw_content,
                result_title=result_title,
                result_url=result_url,
                request_id=request_id,
                extract_depth=depth,
                provider_response_saved_at=saved_at,
                validation=validation,
            )
        )
    return selected


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise AcquisitionStagingError(
            "manifest_schema_mismatch", "Unsupported acquisition manifest schema."
        )
    recorded = str(manifest.get("manifest_sha256") or "")
    unhashed = dict(manifest)
    unhashed.pop("manifest_sha256", None)
    if not recorded or recorded != _canonical_json_hash(unhashed):
        raise AcquisitionStagingError(
            "manifest_hash_mismatch", "Acquisition manifest hash is invalid."
        )
    if manifest.get("run_id") != ACQUISITION_RUN_ID or manifest.get(
        "source_run_id"
    ) != SOURCE_RUN_ID:
        raise AcquisitionStagingError(
            "manifest_run_identity_mismatch",
            "Acquisition manifest run identity does not match the locked run.",
        )
    if manifest.get("constraints") != EXPECTED_MANIFEST_CONSTRAINTS:
        raise AcquisitionStagingError(
            "manifest_constraints_lock_mismatch",
            "Acquisition manifest safety constraints do not match the lock.",
        )
    if manifest.get("inputs") != [dict(value) for value in EXPECTED_INPUT_LOCK]:
        raise AcquisitionStagingError(
            "manifest_input_lock_mismatch",
            "Acquisition manifest input records do not match the four-file lock.",
        )
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != 44:
        raise AcquisitionStagingError(
            "manifest_entry_count_mismatch", "Acquisition manifest must contain 44 entries."
        )
    candidate_ids = [str(entry.get("candidate_id") or "") for entry in entries]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise AcquisitionStagingError(
            "manifest_duplicate_candidate_id",
            "Acquisition manifest candidate IDs must be unique.",
        )
    if set(candidate_ids) != set(EXPECTED_CANDIDATE_IDS):
        raise AcquisitionStagingError(
            "manifest_candidate_set_mismatch",
            "Acquisition manifest does not contain the exact locked candidate set.",
        )
    for entry in entries:
        if entry.get("expected_production_source_id") != entry.get("candidate_id"):
            raise AcquisitionStagingError(
                "manifest_source_identity_mismatch",
                "Expected Production Source ID must equal the locked candidate ID.",
            )


def _preflight_output_paths(
    output_root: Path, artifacts: list[dict[str, Any]]
) -> None:
    """Validate every manifest-derived path before the first output write."""

    seen: set[Path] = set()
    for artifact in artifacts:
        entry = artifact["result"].entry
        candidate_id = entry["candidate_id"]
        paths = (
            _fixed_output_path(
                output_root,
                entry["exact_content"].get("content_file"),
                f"content/{candidate_id}.txt",
            ),
            _fixed_output_path(
                output_root,
                entry["exact_content"].get("receipt_file"),
                f"receipts/{candidate_id}.json",
            ),
            _fixed_output_path(
                output_root,
                entry["production"].get("payload_file"),
                f"source_payloads/{candidate_id}.json",
            ),
        )
        for path in paths:
            if path in seen:
                raise AcquisitionStagingError(
                    "duplicate_output_path", "Two candidates resolve to one output path."
                )
            seen.add(path)
    for relative in ("acquisition-results.json", "run-report.md"):
        path = _fixed_output_path(output_root, relative, relative)
        if path in seen:
            raise AcquisitionStagingError(
                "duplicate_output_path", "Metadata output collides with candidate output."
            )
        seen.add(path)


def _fixed_output_path(output_root: Path, value: object, expected: str) -> Path:
    relative = str(value or "")
    if relative != expected or Path(relative).is_absolute():
        raise AcquisitionStagingError(
            "unsafe_output_path",
            f"Manifest output path must be exactly {expected!r}.",
        )
    root = Path(output_root).resolve()
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AcquisitionStagingError(
            "unsafe_output_path", "Manifest output path escapes the output root."
        ) from exc
    return resolved


def _validate_export_envelope(export: dict[str, Any], filename: str) -> None:
    request_id = export.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise AcquisitionStagingError(
            "tavily_request_id_missing", f"{filename} has no provider request ID."
        )
    if not isinstance(export.get("results"), list) or not isinstance(
        export.get("failed_results"), list
    ):
        raise AcquisitionStagingError(
            "invalid_tavily_export_shape",
            f"{filename} must contain results and failed_results arrays.",
        )


def _result_url_map(values: list[Any], label: str) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict):
            raise AcquisitionStagingError(
                "invalid_tavily_result_shape", f"{label} contains a non-object result."
            )
        url = _canonical_url(value.get("url"))
        if url in mapped:
            raise AcquisitionStagingError(
                "duplicate_tavily_result_url", f"{label} contains duplicate URL {url}."
            )
        mapped[url] = value
    return mapped


def _failed_url_set(values: list[Any], label: str) -> set[str]:
    urls: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            raise AcquisitionStagingError(
                "invalid_tavily_failure_shape", f"{label} has a malformed failure."
            )
        url = _canonical_url(value.get("url"))
        if url in urls:
            raise AcquisitionStagingError(
                "duplicate_tavily_failure_url", f"{label} repeats failed URL {url}."
            )
        urls.add(url)
    return urls


def _locator_for_url(entry: dict[str, Any], url: str) -> dict[str, Any]:
    canonical = _canonical_url(url)
    matches = [
        locator
        for locator in entry["acquisition"]["content_locators"]
        if _canonical_url(locator["url"]) == canonical
    ]
    if len(matches) != 1:
        raise AcquisitionStagingError(
            "result_locator_mismatch", "Result URL does not map to one manifest locator."
        )
    return matches[0]


def _title_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalized)
        if len(token) >= 3 and token not in _TITLE_STOP_WORDS
    }


def _canonical_url(value: object) -> str:
    normalized = str(value or "").strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise AcquisitionStagingError(
            "invalid_result_url", "Tavily result URL is invalid."
        )
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    ).rstrip("/")


def _read_json_object(path: Path, code: str) -> dict[str, Any]:
    if not path.is_file():
        raise AcquisitionStagingError(code, f"Required JSON file is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcquisitionStagingError(code, f"Could not read JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise AcquisitionStagingError(code, f"JSON root must be an object: {path}")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_hash(value: dict[str, Any]) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(serialized)


def _pretty_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"


def _json_bytes(value: dict[str, Any]) -> bytes:
    return _pretty_json(value).encode("utf-8")


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(value)
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)


def _run_report(results: dict[str, Any]) -> str:
    summary = results["summary"]
    request_rows = "\n".join(
        f"- `{item['extract_depth']}` `{item['provider_request_id']}`: "
        f"{item['result_count']} result(s), {item['failed_count']} failure(s)"
        for item in results["inputs"]
    )
    return f"""# Production Source Acquisition 001 — Offline Result Report

This run processed only the three saved Tavily exact-URL exports. No network,
API submission, database write, review decision, unresolved-PDF input, or
persistent vector operation occurred.

## Result

- Selected and validated exact texts: {summary['validated_results']}
- Content files: {summary['content_files']}
- Exact-content receipts: {summary['receipts']}
- Safe SourceVersion payloads: {summary['source_payloads']}
- Receipt byte-hash commitments: {summary['receipts']}
- Payload wrapper byte-hash commitments: {summary['source_payloads']}
- Receipt-only unresolved Source type: {summary['receipt_only']}
- Basic extracts: {summary['basic_results']}
- Advanced fallbacks: {summary['advanced_fallback_results']}

## Provider requests

{request_rows}

## Governance boundary

Every prepared payload retains `requested_extraction_permission=none`,
`requested_storage_permission=none`, `sensitive_information_level=controlled`,
`citation_verification=[]`, `redistribution_allowed=false`, and
`content_access=web_extract`. The saved provider-response file mtime is retained
as timezone-aware `provider_response_saved_at` provenance in every receipt.
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate saved Tavily exports and stage SourceVersion payloads offline."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(
            "research/runs/production-source-acquisition-001/"
            "acquisition-manifest.json"
        ),
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("/tmp/magicforge-source-acquisition-001"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/runs/production-source-acquisition-001"),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        results = process_saved_tavily_exports(
            args.manifest, args.input_dir, args.output_dir
        )
    except AcquisitionStagingError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": str(exc)}))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "output_dir": str(args.output_dir),
                "results_sha256": results["results_sha256"],
                **results["summary"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MINIMUM_CONTENT_CHARACTERS",
    "MINIMUM_TITLE_TOKEN_COVERAGE",
    "RESULTS_SCHEMA_VERSION",
    "process_saved_tavily_exports",
    "validate_exact_result",
    "verify_staged_acquisition_results",
]
