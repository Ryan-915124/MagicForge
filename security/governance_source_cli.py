"""Safely submit prevalidated Source versions to Production governance.

This CLI has one deliberately narrow write surface: ``POST /sources``.  It
cannot review Sources, create Claims or mappings, build manifests, authorize
storage, or write to Qdrant.  All input files are validated before credentials
are requested or an HTTP client is created.

Operator passwords are accepted only through a hidden terminal prompt.  The
Bearer token remains in process memory and is never included in output.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from pydantic import ValidationError

from research.acquisition.staging import (
    EXPECTED_CANDIDATE_IDS,
    EXPECTED_INPUT_LOCK,
    EXPECTED_MANIFEST_CONSTRAINTS,
    AcquisitionStagingError,
    ExactContentReceipt,
)
from research.acquisition.tavily_results import verify_staged_acquisition_results
from research.review.workflow_models import SourceSummary, SourceVersionCommand
from security.governance_account_cli import (
    CliInputError,
    SafeApiError,
    _human_error_detail,
    _normalize_api_url,
    _safe_response_error,
)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PAYLOAD_DIRECTORY = (
    _REPOSITORY_ROOT
    / "research"
    / "runs"
    / "production-source-acquisition-001"
    / "source_payloads"
)
_DEFAULT_RESULTS_FILE = _DEFAULT_PAYLOAD_DIRECTORY.parent / "acquisition-results.json"
_RESULTS_SCHEMA_VERSION = "source-acquisition-results-0.2"
_ACQUISITION_MANIFEST_SCHEMA_VERSION = "source-acquisition-manifest-0.1"
_ACQUISITION_RUN_ID = "production-source-acquisition-001"
_SOURCE_RUN_ID = "magicforge-corpus-run-001"
_OFFICIAL_MANIFEST_SHA256 = (
    "c10e006e6a156ad44024e567d271204f1f38db5de33b791df0a10a15347c17c3"
)
_OFFICIAL_RESULTS_SHA256 = (
    "3e0e637c7df63de1806044d5026c178d1b7eb1494eacd22bced6a0ac904f60e9"
)
_OFFICIAL_RESULTS_SUMMARY: dict[str, int] = {
    "selected_results": 30,
    "validated_results": 30,
    "content_files": 30,
    "receipts": 30,
    "source_payloads": 29,
    "receipt_only": 1,
    "basic_results": 25,
    "advanced_fallback_results": 5,
}
_OFFICIAL_RESULTS_CONSTRAINTS: dict[str, Any] = {
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
}
_UNRESOLVED_SOURCE_TYPE_CANDIDATE_ID = UUID(
    "282bfa90-a30b-5604-9dd5-a046cee0b2fe"
)
_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_PAYLOAD_FILE_BYTES = 12_500_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,254}$")
_WRAPPER_FIELDS = frozenset(
    {
        "candidate_id",
        "expected_production_source_id",
        "content_sha256",
        "idempotency_key",
        "payload",
    }
)
_FORBIDDEN_CONTROL_FIELDS = frozenset(
    {
        "approved",
        "approval",
        "approval_status",
        "approval_reason",
        "review",
        "review_status",
        "review_decision",
        "reviewer",
        "review_date",
        "decision",
        "claim_eligibility",
        "extraction_permission",
        "storage_permission",
        "ingest",
        "ingestion",
        "manifest",
        "qdrant",
    }
)


class PayloadPreflightError(ValueError):
    """A fail-closed payload error with a stable, content-free code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedSourceSubmission:
    """Validated submission material; content is intentionally never rendered."""

    file_name: str
    candidate_id: UUID
    expected_source_id: UUID
    canonical_key: str
    content_sha256: str
    idempotency_key: str
    command: SourceVersionCommand
    payload: Mapping[str, Any]

    def dry_run_record(self) -> dict[str, Any]:
        return {
            "file": self.file_name,
            "candidate_id": str(self.candidate_id),
            "expected_source_id": str(self.expected_source_id),
            "content_sha256": self.content_sha256,
            "status": "ready",
        }


@dataclass(frozen=True, slots=True)
class OfficialPayloadCommitment:
    candidate_id: UUID
    content_sha256: str
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class OfficialAcquisitionBatch:
    """Hash-bound allowlist derived from one acquisition results document."""

    results_file: Path
    results_sha256: str
    source_manifest_sha256: str
    expected_by_file: Mapping[str, OfficialPayloadCommitment]

    def report_record(self) -> dict[str, Any]:
        return {
            "run_id": _ACQUISITION_RUN_ID,
            "results_sha256": self.results_sha256,
            "source_manifest_sha256": self.source_manifest_sha256,
            "payload_count": len(self.expected_by_file),
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m security.governance_source_cli",
        description=(
            "Preflight governed Source payloads and, only with --execute, "
            "submit them using a single-role Production operator account."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser(
        "submit",
        help="Validate every payload; dry-run unless --execute is present.",
    )
    submit.add_argument("--api-url", default="http://127.0.0.1:8000")
    submit.add_argument("--operator-identifier", required=True)
    submit.add_argument(
        "--payload-dir",
        type=Path,
        default=_DEFAULT_PAYLOAD_DIRECTORY,
        help=(
            "Directory containing acquisition wrapper JSON files "
            "(default: production-source-acquisition-001/source_payloads)."
        ),
    )
    submit.add_argument(
        "--results-file",
        type=Path,
        default=_DEFAULT_RESULTS_FILE,
        help=(
            "Hash-bearing acquisition-results.json that defines the complete "
            "official prepared payload set. This check cannot be disabled."
        ),
    )
    submit.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Perform login and POST /sources. Without this flag the command "
            "is an offline dry-run."
        ),
    )
    submit.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Write one machine-readable, content-free result object.",
    )
    return parser


def _new_client(api_url: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"{api_url.rstrip('/')}/",
        timeout=_REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
        headers={
            "Accept": "application/json",
            "User-Agent": "MagicForge-Governance-Source-Submitter/1.0",
        },
    )


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_non_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _load_json_object(path: Path) -> dict[str, Any]:
    raw = _read_bounded_bytes(path, "payload_file_unreadable")
    return _load_json_bytes_object(raw)


def _read_bounded_bytes(path: Path, error_code: str) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PayloadPreflightError(error_code) from exc
    if size > _MAX_PAYLOAD_FILE_BYTES:
        raise PayloadPreflightError("payload_file_too_large")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PayloadPreflightError(error_code) from exc


def _load_json_bytes_object(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_non_json_constant,
        )
    except _DuplicateJsonKey as exc:
        raise PayloadPreflightError("duplicate_json_key") from exc
    except (
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise PayloadPreflightError("invalid_payload_json") from exc
    if not isinstance(value, dict):
        raise PayloadPreflightError("payload_wrapper_not_object")
    return value


def _find_forbidden_control_field(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).casefold() in _FORBIDDEN_CONTROL_FIELDS:
                return True
            if _find_forbidden_control_field(nested):
                return True
    elif isinstance(value, list):
        return any(_find_forbidden_control_field(item) for item in value)
    return False


def _required_string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PayloadPreflightError(code)
    return value.strip()


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise PayloadPreflightError("invalid_source_identity")
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    ).rstrip("/")


def _source_identity(command: SourceVersionCommand) -> tuple[str, str, UUID]:
    if command.citation.doi:
        identity = command.citation.doi
        canonical_key = f"doi:{identity}"
    else:
        identity = _canonical_url(command.citation.url)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        canonical_key = f"url-sha256:{digest}"
    source_id = uuid5(NAMESPACE_URL, f"magicforge:research:{identity}")
    return identity, canonical_key, source_id


def _canonical_json_hash(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise PayloadPreflightError("invalid_hashable_json") from exc
    return hashlib.sha256(encoded).hexdigest()


def _verified_embedded_hash(
    document: Mapping[str, Any],
    field: str,
    mismatch_code: str,
) -> str:
    recorded = document.get(field)
    if not isinstance(recorded, str) or not _SHA256.fullmatch(recorded):
        raise PayloadPreflightError(mismatch_code)
    unhashed = dict(document)
    unhashed.pop(field, None)
    if _canonical_json_hash(unhashed) != recorded:
        raise PayloadPreflightError(mismatch_code)
    return recorded


def _fixed_run_file(run_root: Path, value: object, expected: str) -> Path:
    if value != expected:
        raise PayloadPreflightError("acquisition_artifact_path_mismatch")
    candidate = run_root / expected
    if candidate.is_symlink():
        raise PayloadPreflightError("unsafe_acquisition_artifact")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(run_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PayloadPreflightError("acquisition_artifact_missing") from exc
    if not resolved.is_file():
        raise PayloadPreflightError("acquisition_artifact_missing")
    return resolved


def _validate_locked_manifest(
    source_manifest: Mapping[str, Any],
) -> dict[UUID, Mapping[str, Any]]:
    if (
        source_manifest.get("schema_version")
        != _ACQUISITION_MANIFEST_SCHEMA_VERSION
        or source_manifest.get("run_id") != _ACQUISITION_RUN_ID
        or source_manifest.get("source_run_id") != _SOURCE_RUN_ID
    ):
        raise PayloadPreflightError("source_manifest_identity_mismatch")
    if source_manifest.get("constraints") != EXPECTED_MANIFEST_CONSTRAINTS:
        raise PayloadPreflightError("source_manifest_constraints_lock_mismatch")
    if source_manifest.get("inputs") != [dict(value) for value in EXPECTED_INPUT_LOCK]:
        raise PayloadPreflightError("source_manifest_input_lock_mismatch")
    entries = source_manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != 44:
        raise PayloadPreflightError("source_manifest_entry_count_mismatch")
    by_id: dict[UUID, Mapping[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise PayloadPreflightError("source_manifest_entry_invalid")
        candidate_text = entry.get("candidate_id")
        if not isinstance(candidate_text, str):
            raise PayloadPreflightError("source_manifest_candidate_invalid")
        try:
            candidate_id = UUID(candidate_text)
        except ValueError as exc:
            raise PayloadPreflightError("source_manifest_candidate_invalid") from exc
        if candidate_id in by_id:
            raise PayloadPreflightError("source_manifest_duplicate_candidate")
        if entry.get("expected_production_source_id") != str(candidate_id):
            raise PayloadPreflightError("source_manifest_source_identity_mismatch")
        by_id[candidate_id] = entry
    if {str(candidate_id) for candidate_id in by_id} != set(
        EXPECTED_CANDIDATE_IDS
    ):
        raise PayloadPreflightError("source_manifest_candidate_set_mismatch")
    return by_id


def _validate_receipt_commitment(
    run_root: Path,
    item: Mapping[str, Any],
    candidate_id: UUID,
) -> None:
    candidate_text = str(candidate_id)
    content_sha256 = item.get("content_sha256")
    receipt_sha256 = item.get("receipt_sha256")
    content_bytes_expected = item.get("content_bytes")
    if (
        not isinstance(content_sha256, str)
        or not _SHA256.fullmatch(content_sha256)
        or not isinstance(receipt_sha256, str)
        or not _SHA256.fullmatch(receipt_sha256)
        or not isinstance(content_bytes_expected, int)
        or isinstance(content_bytes_expected, bool)
        or content_bytes_expected < 1
    ):
        raise PayloadPreflightError("receipt_commitment_fields_invalid")
    content_path = _fixed_run_file(
        run_root,
        item.get("content_file"),
        f"content/{candidate_text}.txt",
    )
    receipt_path = _fixed_run_file(
        run_root,
        item.get("receipt_file"),
        f"receipts/{candidate_text}.json",
    )
    content_bytes = _read_bounded_bytes(content_path, "content_file_unreadable")
    receipt_bytes = _read_bounded_bytes(receipt_path, "receipt_file_unreadable")
    if (
        hashlib.sha256(content_bytes).hexdigest() != content_sha256
        or len(content_bytes) != content_bytes_expected
    ):
        raise PayloadPreflightError("content_file_commitment_mismatch")
    if hashlib.sha256(receipt_bytes).hexdigest() != receipt_sha256:
        raise PayloadPreflightError("receipt_file_hash_mismatch")
    try:
        receipt_payload = _load_json_bytes_object(receipt_bytes)
        receipt = ExactContentReceipt.model_validate(receipt_payload)
    except (PayloadPreflightError, ValidationError, RecursionError) as exc:
        raise PayloadPreflightError("invalid_exact_content_receipt") from exc
    saved_at = item.get("provider_response_saved_at")
    if not isinstance(saved_at, str):
        raise PayloadPreflightError("receipt_semantics_mismatch")
    try:
        item_saved_at = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadPreflightError("receipt_semantics_mismatch") from exc
    validation = item.get("validation")
    if (
        receipt.candidate_id != candidate_id
        or receipt.content_file != f"content/{candidate_text}.txt"
        or receipt.content_access.value != "web_extract"
        or receipt.acquisition_method
        != f"tavily_exact_url_extract:{item.get('extract_depth')}"
        or receipt.content_sha256 != content_sha256
        or receipt.content_bytes != content_bytes_expected
        or receipt.provider != "tavily"
        or receipt.provider_request_id != item.get("provider_request_id")
        or receipt.extract_depth != item.get("extract_depth")
        or receipt.provider_result_title != item.get("result_title")
        or receipt.provider_response_saved_at != item_saved_at
        or receipt.source_locator != item.get("result_url")
        or receipt.returned_url != item.get("result_url")
        or receipt.validation != validation
        or receipt.validation.get("passed") is not True
        or receipt.access.redistribution_allowed is not False
    ):
        raise PayloadPreflightError("receipt_semantics_mismatch")


def _load_official_batch(
    results_file: Path,
    payload_directory: Path,
) -> OfficialAcquisitionBatch:
    unresolved_results = results_file.expanduser()
    if unresolved_results.is_symlink():
        raise PayloadPreflightError("unsafe_acquisition_results_file")
    try:
        resolved_results = unresolved_results.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PayloadPreflightError("acquisition_results_missing") from exc
    if (
        not resolved_results.is_file()
        or resolved_results.name != "acquisition-results.json"
    ):
        raise PayloadPreflightError("unsafe_acquisition_results_file")
    run_root = resolved_results.parent.resolve()
    expected_payload_directory = (run_root / "source_payloads").resolve()
    if payload_directory != expected_payload_directory:
        raise PayloadPreflightError("results_payload_directory_mismatch")

    try:
        results = _load_json_object(resolved_results)
    except PayloadPreflightError as exc:
        raise PayloadPreflightError("invalid_acquisition_results_json") from exc
    if results.get("schema_version") != _RESULTS_SCHEMA_VERSION:
        raise PayloadPreflightError("acquisition_results_schema_mismatch")
    if results.get("run_id") != _ACQUISITION_RUN_ID:
        raise PayloadPreflightError("acquisition_results_run_mismatch")
    results_sha256 = _verified_embedded_hash(
        results,
        "results_sha256",
        "acquisition_results_hash_mismatch",
    )
    if results_sha256 != _OFFICIAL_RESULTS_SHA256:
        raise PayloadPreflightError("acquisition_results_trust_anchor_mismatch")

    source_manifest_name = results.get("source_manifest")
    if source_manifest_name != "acquisition-manifest.json":
        raise PayloadPreflightError("source_manifest_name_mismatch")
    source_manifest_sha256 = results.get("source_manifest_sha256")
    if source_manifest_sha256 != _OFFICIAL_MANIFEST_SHA256:
        raise PayloadPreflightError("source_manifest_trust_anchor_mismatch")
    source_manifest_path = _fixed_run_file(
        run_root,
        source_manifest_name,
        "acquisition-manifest.json",
    )
    try:
        source_manifest = _load_json_object(source_manifest_path)
    except PayloadPreflightError as exc:
        raise PayloadPreflightError("source_manifest_invalid") from exc
    verified_manifest_hash = _verified_embedded_hash(
        source_manifest,
        "manifest_sha256",
        "source_manifest_hash_mismatch",
    )
    if verified_manifest_hash != _OFFICIAL_MANIFEST_SHA256:
        raise PayloadPreflightError("source_manifest_trust_anchor_mismatch")
    manifest_by_id = _validate_locked_manifest(source_manifest)

    if results.get("constraints") != _OFFICIAL_RESULTS_CONSTRAINTS:
        raise PayloadPreflightError("acquisition_results_constraints_unsafe")
    items = results.get("items")
    if (
        not isinstance(items, list)
        or len(items) != 30
        or results.get("summary") != _OFFICIAL_RESULTS_SUMMARY
    ):
        raise PayloadPreflightError("acquisition_results_shape_invalid")

    ready_candidate_ids = {
        candidate_id
        for candidate_id, entry in manifest_by_id.items()
        if isinstance(entry.get("acquisition"), Mapping)
        and entry["acquisition"].get("status") == "ready"
    }
    ready_academic = {
        candidate_id
        for candidate_id in ready_candidate_ids
        if manifest_by_id[candidate_id].get("source_category") == "academic"
    }
    if (
        len(ready_candidate_ids) != 30
        or len(ready_academic) != 18
        or len(ready_candidate_ids - ready_academic) != 12
    ):
        raise PayloadPreflightError("source_manifest_ready_set_mismatch")

    normalized_items: list[tuple[Mapping[str, Any], UUID]] = []
    item_candidate_ids: list[UUID] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise PayloadPreflightError("acquisition_result_item_invalid")
        candidate_text = item.get("candidate_id")
        if not isinstance(candidate_text, str):
            raise PayloadPreflightError("acquisition_result_candidate_invalid")
        try:
            candidate_id = UUID(candidate_text)
        except ValueError as exc:
            raise PayloadPreflightError("acquisition_result_candidate_invalid") from exc
        item_candidate_ids.append(candidate_id)
        normalized_items.append((item, candidate_id))
    if (
        len(item_candidate_ids) != len(set(item_candidate_ids))
        or set(item_candidate_ids) != ready_candidate_ids
    ):
        raise PayloadPreflightError("acquisition_result_candidate_set_mismatch")

    expected_prepared_ids = ready_candidate_ids - {
        _UNRESOLVED_SOURCE_TYPE_CANDIDATE_ID
    }
    expected_by_file: dict[str, OfficialPayloadCommitment] = {}
    prepared_candidate_ids: set[UUID] = set()
    receipt_only_candidate_ids: set[UUID] = set()
    for item, candidate_id in normalized_items:
        manifest_entry = manifest_by_id[candidate_id]
        validation = item.get("validation")
        if (
            item.get("provider") != "tavily"
            or item.get("source_category")
            != manifest_entry.get("source_category")
            or not isinstance(validation, Mapping)
            or validation.get("passed") is not True
        ):
            raise PayloadPreflightError("acquisition_result_item_invalid")
        _validate_receipt_commitment(run_root, item, candidate_id)

        classification = manifest_entry.get("classification")
        if not isinstance(classification, Mapping):
            raise PayloadPreflightError("source_manifest_classification_invalid")
        status = item.get("payload_status")
        if status == "prepared":
            content_sha256 = item.get("content_sha256")
            payload_sha256 = item.get("payload_sha256")
            payload_file = item.get("payload_file")
            if (
                candidate_id not in expected_prepared_ids
                or not isinstance(content_sha256, str)
                or not _SHA256.fullmatch(content_sha256)
                or not isinstance(payload_sha256, str)
                or not _SHA256.fullmatch(payload_sha256)
                or payload_file != f"source_payloads/{candidate_id}.json"
                or item.get("payload_blocker") is not None
                or classification.get("source_type") is None
                or classification.get("requires_human_source_type") is not False
            ):
                raise PayloadPreflightError("prepared_result_item_invalid")
            payload_path = _fixed_run_file(
                run_root,
                payload_file,
                f"source_payloads/{candidate_id}.json",
            )
            payload_bytes = _read_bounded_bytes(
                payload_path, "payload_file_unreadable"
            )
            if hashlib.sha256(payload_bytes).hexdigest() != payload_sha256:
                raise PayloadPreflightError("payload_file_hash_mismatch")
            file_name = f"{candidate_id}.json"
            expected_by_file[file_name] = OfficialPayloadCommitment(
                candidate_id=candidate_id,
                content_sha256=content_sha256,
                payload_sha256=payload_sha256,
            )
            prepared_candidate_ids.add(candidate_id)
        elif status == "receipt_only_source_type_unresolved":
            if (
                candidate_id != _UNRESOLVED_SOURCE_TYPE_CANDIDATE_ID
                or item.get("payload_file") is not None
                or item.get("payload_sha256") is not None
                or item.get("payload_blocker")
                != "source_type_human_choice_required"
                or classification.get("source_type") is not None
                or classification.get("requires_human_source_type") is not True
                or classification.get("source_role")
                != "unofficial_interview_archive"
            ):
                raise PayloadPreflightError("receipt_only_result_invalid")
            if (payload_directory / f"{candidate_id}.json").exists():
                raise PayloadPreflightError("unexpected_receipt_only_payload")
            receipt_only_candidate_ids.add(candidate_id)
        else:
            raise PayloadPreflightError("acquisition_result_status_invalid")

    if (
        prepared_candidate_ids != expected_prepared_ids
        or receipt_only_candidate_ids
        != {_UNRESOLVED_SOURCE_TYPE_CANDIDATE_ID}
        or len(expected_by_file) != 29
    ):
        raise PayloadPreflightError("official_payload_candidate_set_mismatch")

    try:
        verification = verify_staged_acquisition_results(
            run_root,
            results_document=dict(results),
        )
    except AcquisitionStagingError as exc:
        safe_code = (
            exc.code
            if isinstance(exc.code, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", exc.code)
            else "staged_bundle_verification_failed"
        )
        raise PayloadPreflightError(safe_code) from exc
    if verification != {
        "items_verified": 30,
        "receipt_hashes_verified": 30,
        "payload_hashes_verified": 29,
        "receipt_only_verified": 1,
    }:
        raise PayloadPreflightError("staged_bundle_verification_mismatch")

    return OfficialAcquisitionBatch(
        results_file=resolved_results,
        results_sha256=results_sha256,
        source_manifest_sha256=source_manifest_sha256,
        expected_by_file=expected_by_file,
    )


def _prepare_payload(
    path: Path,
    *,
    expected_payload_sha256: str | None = None,
) -> PreparedSourceSubmission:
    payload_bytes = _read_bounded_bytes(path, "payload_file_unreadable")
    if (
        expected_payload_sha256 is not None
        and hashlib.sha256(payload_bytes).hexdigest() != expected_payload_sha256
    ):
        raise PayloadPreflightError("payload_file_hash_mismatch")
    wrapper = _load_json_bytes_object(payload_bytes)
    if frozenset(wrapper) != _WRAPPER_FIELDS:
        raise PayloadPreflightError("invalid_payload_wrapper_fields")
    try:
        forbidden_control_field = _find_forbidden_control_field(wrapper)
    except RecursionError as exc:
        raise PayloadPreflightError("payload_nesting_too_deep") from exc
    if forbidden_control_field:
        raise PayloadPreflightError("forbidden_governance_control_field")

    candidate_text = _required_string(wrapper.get("candidate_id"), "invalid_candidate_id")
    expected_text = _required_string(
        wrapper.get("expected_production_source_id"),
        "invalid_expected_source_id",
    )
    try:
        candidate_id = UUID(candidate_text)
        expected_source_id = UUID(expected_text)
    except ValueError as exc:
        raise PayloadPreflightError("invalid_source_identifier") from exc
    if path.name != f"{candidate_id}.json":
        raise PayloadPreflightError("payload_filename_mismatch")
    if candidate_id != expected_source_id:
        raise PayloadPreflightError("candidate_source_id_mismatch")

    content_sha256 = _required_string(
        wrapper.get("content_sha256"), "invalid_content_sha256"
    )
    if not _SHA256.fullmatch(content_sha256):
        raise PayloadPreflightError("invalid_content_sha256")
    idempotency_key = _required_string(
        wrapper.get("idempotency_key"), "invalid_idempotency_key"
    )
    if not _SAFE_IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise PayloadPreflightError("invalid_idempotency_key")

    payload = wrapper.get("payload")
    if not isinstance(payload, dict):
        raise PayloadPreflightError("source_command_not_object")
    try:
        command = SourceVersionCommand.model_validate(payload)
    except (ValidationError, RecursionError) as exc:
        # Pydantic's rendered error may contain Source content, so it must not
        # cross the CLI output boundary.
        raise PayloadPreflightError("invalid_source_version_command") from exc

    if command.requested_extraction_permission.value != "none":
        raise PayloadPreflightError("extraction_permission_not_none")
    if command.requested_storage_permission.value != "none":
        raise PayloadPreflightError("storage_permission_not_none")
    if command.requested_scope_locators:
        raise PayloadPreflightError("scope_locators_not_empty")
    if command.citation_verification:
        raise PayloadPreflightError("citation_verification_not_empty")
    if command.sensitive_information_level.value != "controlled":
        raise PayloadPreflightError("sensitivity_not_controlled")
    if command.access.redistribution_allowed is not False:
        raise PayloadPreflightError("redistribution_must_be_false")
    if command.content_access.value != "web_extract":
        raise PayloadPreflightError("content_access_not_web_extract")

    computed_hash = hashlib.sha256(command.content.encode("utf-8")).hexdigest()
    if computed_hash != content_sha256:
        raise PayloadPreflightError("content_hash_mismatch")
    expected_key = f"corpus-run-001:source:{candidate_id}:{computed_hash}"
    if idempotency_key != expected_key:
        raise PayloadPreflightError("idempotency_key_mismatch")

    _identity, canonical_key, deterministic_source_id = _source_identity(command)
    if deterministic_source_id != expected_source_id:
        raise PayloadPreflightError("source_identity_mismatch")

    return PreparedSourceSubmission(
        file_name=path.name,
        candidate_id=candidate_id,
        expected_source_id=expected_source_id,
        canonical_key=canonical_key,
        content_sha256=content_sha256,
        idempotency_key=idempotency_key,
        command=command,
        # Preserve the exact validated JSON representation.  Re-dumping a
        # Pydantic model could materialize a default provenance timestamp that
        # was absent from the file, changing retry identity between runs.
        payload=payload,
    )


def _preflight_directory(
    payload_directory: Path,
    results_file: Path,
) -> tuple[
    list[PreparedSourceSubmission],
    list[dict[str, str]],
    OfficialAcquisitionBatch | None,
]:
    try:
        root = payload_directory.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CliInputError("Payload directory does not exist.") from exc
    if not root.is_dir():
        raise CliInputError("Payload path must be a directory.")
    try:
        batch = _load_official_batch(results_file, root)
    except PayloadPreflightError as exc:
        return (
            [],
            [{"file": "acquisition-results.json", "code": exc.code}],
            None,
        )
    try:
        files = sorted(root.glob("*.json"), key=lambda item: item.name)
    except OSError as exc:
        raise CliInputError("Payload directory cannot be read.") from exc
    actual_names = {path.name for path in files}
    expected_names = set(batch.expected_by_file)

    prepared: list[PreparedSourceSubmission] = []
    errors: list[dict[str, str]] = []
    for missing in sorted(expected_names - actual_names):
        errors.append({"file": missing, "code": "prepared_payload_missing"})
    for extra in sorted(actual_names - expected_names):
        errors.append({"file": extra, "code": "unexpected_payload_file"})
    for path in files:
        if path.name not in expected_names:
            continue
        if path.is_symlink() or not path.is_file():
            errors.append({"file": path.name, "code": "unsafe_payload_file"})
            continue
        try:
            commitment = batch.expected_by_file[path.name]
            submission = _prepare_payload(
                path,
                expected_payload_sha256=commitment.payload_sha256,
            )
            if submission.candidate_id != commitment.candidate_id:
                raise PayloadPreflightError("results_candidate_id_mismatch")
            if submission.content_sha256 != commitment.content_sha256:
                raise PayloadPreflightError("results_content_hash_mismatch")
            prepared.append(submission)
        except PayloadPreflightError as exc:
            errors.append({"file": path.name, "code": exc.code})

    duplicate_errors = _duplicate_submission_errors(prepared)
    errors.extend(duplicate_errors)
    return (
        prepared,
        sorted(errors, key=lambda item: (item["file"], item["code"])),
        batch,
    )


def _duplicate_submission_errors(
    prepared: Sequence[PreparedSourceSubmission],
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for attribute, code in (
        ("candidate_id", "duplicate_candidate_id"),
        ("expected_source_id", "duplicate_expected_source_id"),
        ("canonical_key", "duplicate_source_identity"),
        ("idempotency_key", "duplicate_idempotency_key"),
    ):
        seen: dict[object, str] = {}
        for item in prepared:
            value = getattr(item, attribute)
            previous = seen.get(value)
            if previous is not None:
                errors.append({"file": previous, "code": code})
                errors.append({"file": item.file_name, "code": code})
            else:
                seen[value] = item.file_name
    unique = {(item["file"], item["code"]): item for item in errors}
    return list(unique.values())


def _post_json(
    client: httpx.Client,
    path: str,
    payload: Mapping[str, Any],
    *,
    token: str | None = None,
    idempotency_key: str | None = None,
    expected_status: int,
    allow_replay_status: bool = False,
) -> tuple[Mapping[str, Any], bool]:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    try:
        response = client.post(path.lstrip("/"), json=dict(payload), headers=headers)
    except httpx.TimeoutException as exc:
        raise SafeApiError("request_timeout") from exc
    except httpx.HTTPError as exc:
        raise SafeApiError("request_failed") from exc

    replay = False
    if response.status_code != expected_status:
        replay_header = response.headers.get("Idempotency-Replayed", "")
        if (
            allow_replay_status
            and response.status_code == 200
            and replay_header.casefold() == "true"
        ):
            replay = True
        else:
            raise _safe_response_error(response)
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise SafeApiError("invalid_api_response") from exc
    if not isinstance(body, Mapping):
        raise SafeApiError("invalid_api_response")
    return body, replay


def _operator_login(
    client: httpx.Client,
    *,
    identifier: str,
    password: str,
) -> str:
    body, _replay = _post_json(
        client,
        "/auth/login",
        {
            "identifier": identifier,
            "password": password,
            "transport": "bearer",
            "device_label": "governance-source-cli",
        },
        expected_status=200,
    )
    token = body.get("access_token")
    actor = body.get("actor")
    roles = actor.get("roles") if isinstance(actor, Mapping) else None
    if (
        body.get("transport") != "bearer"
        or not isinstance(token, str)
        or not token
        or roles != ["operator"]
    ):
        # Attach a token only to the exception instance so the caller can
        # revoke a server-created session without ever rendering it.
        error = SafeApiError("operator_login_response_invalid")
        error.session_token = token if isinstance(token, str) and token else None
        raise error
    return token


def _validate_source_response(
    body: Mapping[str, Any],
    submission: PreparedSourceSubmission,
) -> dict[str, Any]:
    try:
        summary = SourceSummary.model_validate(body)
    except ValidationError as exc:
        raise SafeApiError("invalid_source_response") from exc
    if (
        summary.id != submission.expected_source_id
        or summary.canonical_key != submission.canonical_key
        or summary.title != submission.command.citation.title
        or summary.source_category != submission.command.source_category
    ):
        raise SafeApiError("source_response_identity_mismatch")
    matching = [
        version
        for version in summary.versions
        if version.content_hash == submission.content_sha256
    ]
    if len(summary.versions) != 1 or len(matching) != 1:
        raise SafeApiError("source_response_version_mismatch")
    version = matching[0]
    expected_version_id = uuid5(
        NAMESPACE_URL,
        f"magicforge:source-version:{summary.id}:{submission.content_sha256}",
    )
    if (
        version.id != expected_version_id
        or version.version != 1
        or version.status.value != "submitted"
        or version.citation_status != "unverified"
        or version.sensitivity.value != "controlled"
        or version.content_access != submission.command.content_access
        or version.source_type != submission.command.source_type
        or version.knowledge_origin != submission.command.knowledge_origin
    ):
        raise SafeApiError("source_response_version_mismatch")
    return {
        "source_id": str(summary.id),
        "source_version_id": str(version.id),
        "version": version.version,
        "content_sha256": version.content_hash,
        "workflow_status": version.status.value,
    }


def _execute_submissions(
    client: httpx.Client,
    *,
    operator_identifier: str,
    operator_password: str,
    submissions: Sequence[PreparedSourceSubmission],
    batch: OfficialAcquisitionBatch,
) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {
        "status": "failed",
        "mode": "execute",
        "batch": batch.report_record(),
        "preflight": {"status": "passed", "payload_count": len(submissions)},
        "login": {"status": "not_attempted"},
        "submissions": [
            {
                "file": item.file_name,
                "candidate_id": str(item.candidate_id),
                "status": "not_attempted",
            }
            for item in submissions
        ],
        "logout": {"status": "not_attempted"},
        "source_requests_sent": 0,
    }
    token: str | None = None
    try:
        try:
            token = _operator_login(
                client,
                identifier=operator_identifier,
                password=operator_password,
            )
        except SafeApiError as exc:
            token = getattr(exc, "session_token", None)
            report["login"] = {"status": "failed", "error": exc.as_dict()}
            return 1, report
        report["login"] = {"status": "succeeded", "roles": ["operator"]}

        for index, submission in enumerate(submissions):
            report["source_requests_sent"] += 1
            try:
                body, replay = _post_json(
                    client,
                    "/sources",
                    submission.payload,
                    token=token,
                    idempotency_key=submission.idempotency_key,
                    expected_status=201,
                    allow_replay_status=True,
                )
                result = _validate_source_response(body, submission)
                report["submissions"][index] = {
                    "file": submission.file_name,
                    "candidate_id": str(submission.candidate_id),
                    # The current FastAPI contract returns 201 for both the
                    # initial registration and its cached replay.  Without an
                    # explicit replay header, report that uncertainty instead
                    # of claiming a new write occurred.
                    "status": (
                        "idempotent_replay"
                        if replay
                        else "accepted_or_idempotent_replay"
                    ),
                    **result,
                }
            except SafeApiError as exc:
                report["submissions"][index] = {
                    "file": submission.file_name,
                    "candidate_id": str(submission.candidate_id),
                    "status": "failed",
                    "error": exc.as_dict(),
                }
    finally:
        operator_password = ""
        if token is not None:
            try:
                body, _replay = _post_json(
                    client,
                    "/auth/logout",
                    {},
                    token=token,
                    expected_status=200,
                )
                if body.get("revoked") is not True:
                    raise SafeApiError("logout_not_revoked")
                report["logout"] = {
                    "status": "succeeded",
                    "revoked": body["revoked"],
                }
            except SafeApiError as exc:
                report["logout"] = {
                    "status": "failed",
                    "error": exc.as_dict(),
                }
            token = ""

    statuses = {item["status"] for item in report["submissions"]}
    accepted_statuses = {
        "accepted_or_idempotent_replay",
        "idempotent_replay",
    }
    accepted = statuses <= accepted_statuses
    logout_succeeded = report["logout"]["status"] == "succeeded"
    if accepted and logout_succeeded:
        report["status"] = "succeeded"
        return 0, report
    if statuses & accepted_statuses:
        report["status"] = "partial_failure"
    else:
        report["status"] = "failed"
    return 1, report


def _dry_run_report(
    api_url: str,
    operator_identifier: str,
    submissions: Sequence[PreparedSourceSubmission],
    batch: OfficialAcquisitionBatch,
) -> dict[str, Any]:
    return {
        "status": "dry_run",
        "mode": "dry_run",
        "api_url": api_url,
        "operator_identifier": operator_identifier,
        "batch": batch.report_record(),
        "preflight": {"status": "passed", "payload_count": len(submissions)},
        "submissions": [item.dry_run_record() for item in submissions],
        "source_requests_sent": 0,
    }


def _human_lines(report: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    output: list[str] = []
    errors: list[str] = []
    if report.get("status") == "dry_run":
        count = report.get("preflight", {}).get("payload_count", 0)
        output.append(f"Dry run passed for {count} Source payload(s); no requests sent.")
        output.append("Re-run with --execute to submit exactly POST /sources.")
        return output, errors

    preflight = report.get("preflight")
    if isinstance(preflight, Mapping) and preflight.get("status") == "failed":
        for item in preflight.get("errors", []):
            if isinstance(item, Mapping):
                errors.append(
                    f"Preflight failed for {item.get('file', 'payload')}: "
                    f"{item.get('code', 'invalid_payload')}."
                )
        return output, errors

    login = report.get("login")
    if isinstance(login, Mapping) and login.get("status") == "failed":
        errors.append(
            "Operator login failed: "
            f"{_human_error_detail(login.get('error', {}))}"
        )
    for item in report.get("submissions", []):
        if not isinstance(item, Mapping):
            continue
        if item.get("status") in {
            "accepted_or_idempotent_replay",
            "idempotent_replay",
        }:
            output.append(
                f"{item['status']}: Source {item['source_id']} version "
                f"{item['version']} ({item['workflow_status']})."
            )
        elif item.get("status") == "failed":
            errors.append(
                f"Submission failed for {item.get('file', 'payload')}: "
                f"{_human_error_detail(item.get('error', {}))}"
            )
    logout = report.get("logout")
    if isinstance(logout, Mapping) and logout.get("status") == "succeeded":
        output.append("Operator API session logout completed.")
    elif isinstance(logout, Mapping) and logout.get("status") == "failed":
        errors.append(
            "Operator logout failed: "
            f"{_human_error_detail(logout.get('error', {}))}"
        )
    return output, errors


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        api_url = _normalize_api_url(arguments.api_url)
        operator_identifier = arguments.operator_identifier.strip()
        if not operator_identifier:
            raise CliInputError("Operator identifier must not be empty.")
        submissions, preflight_errors, batch = _preflight_directory(
            arguments.payload_dir,
            arguments.results_file,
        )
    except CliInputError as exc:
        report: dict[str, Any] = {
            "status": "failed",
            "mode": "execute" if arguments.execute else "dry_run",
            "error": {"code": "invalid_input", "message": str(exc)},
            "source_requests_sent": 0,
        }
        exit_code = 2
    else:
        if preflight_errors:
            report = {
                "status": "failed",
                "mode": "execute" if arguments.execute else "dry_run",
                "preflight": {
                    "status": "failed",
                    "payload_count": len(
                        {item.file_name for item in submissions}
                        | {item["file"] for item in preflight_errors}
                    ),
                    "errors": preflight_errors,
                },
                "source_requests_sent": 0,
            }
            exit_code = 2
        elif not arguments.execute:
            assert batch is not None
            report = _dry_run_report(
                api_url,
                operator_identifier,
                submissions,
                batch,
            )
            exit_code = 0
        else:
            assert batch is not None
            operator_password = ""
            try:
                operator_password = getpass.getpass("Operator password: ")
                if not operator_password:
                    raise CliInputError("Operator password must not be empty.")
                with _new_client(api_url) as client:
                    exit_code, report = _execute_submissions(
                        client,
                        operator_identifier=operator_identifier,
                        operator_password=operator_password,
                        submissions=submissions,
                        batch=batch,
                    )
            except CliInputError as exc:
                report = {
                    "status": "failed",
                    "mode": "execute",
                    "error": {"code": "invalid_input", "message": str(exc)},
                    "source_requests_sent": 0,
                }
                exit_code = 2
            finally:
                operator_password = ""

    if arguments.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        output, errors = _human_lines(report)
        for line in output:
            print(line)
        for line in errors:
            print(line, file=sys.stderr)
        if not output and not errors and report.get("error"):
            print(
                f"Source submission failed: {report['error']['message']}",
                file=sys.stderr,
            )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
