"""Build the fixed Corpus Run 001 Source-acquisition staging manifest.

This module is intentionally offline.  It performs no discovery, retrieval,
API submission, review, approval, extraction, or database write.  It only:

* joins the four immutable discovery/verification JSON inputs for the existing
  44 candidates;
* describes where independently acquired exact content must later be placed;
* verifies an exact-content receipt before compiling a Production
  ``SourceVersionCommand`` payload.

Candidate ``content``/``snippet`` fields and all Bootstrap claim/evidence
artifacts are outside the trust boundary and can never be used as a fallback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from knowledge.evidence import KnowledgeOrigin, SourceType
from knowledge.governance import (
    ExtractionPermission,
    SensitiveInformationLevel,
    StoragePermission,
)
from research.models import ContentAccess, ResearchCandidate, SourceCategory
from research.review.workflow_models import (
    CitationMetadataInput,
    SourceAccessMetadata,
    SourceVersionCommand,
)


MANIFEST_SCHEMA_VERSION = "source-acquisition-manifest-0.1"
RECEIPT_SCHEMA_VERSION = "exact-source-content-receipt-0.1"
SOURCE_RUN_ID = "magicforge-corpus-run-001"
ACQUISITION_RUN_ID = "production-source-acquisition-001"
EXPECTED_CANDIDATE_COUNT = 44

EXPECTED_INPUT_LOCK: tuple[dict[str, Any], ...] = (
    {
        "role": "academic_candidates",
        "path": "candidates/academic-candidates.json",
        "sha256": "030e10f0c462eaf490fd8f2e9b13edb389e0a77a26da11e26579425ae4ebd532",
        "record_count": 32,
    },
    {
        "role": "practitioner_candidates",
        "path": "candidates/practitioner-candidates.json",
        "sha256": "a8da3771cf05d97287f8b7c9300310942a82e7a35d48b90f95ebd3a022018e57",
        "record_count": 12,
    },
    {
        "role": "verified_academic_sources",
        "path": "sources/verified-academic-sources.json",
        "sha256": "ab4e5d2f939499590186ba9a3838c447da22e3b28826383b5dba5faed47dac6b",
        "record_count": 32,
    },
    {
        "role": "access_checked_practitioner_sources",
        "path": "sources/access-checked-practitioner-sources.json",
        "sha256": "a6df4dd22075e87a4ec87dbb91b18789b0671b91482f447700def943bb6c4d72",
        "record_count": 12,
    },
)

EXPECTED_MANIFEST_CONSTRAINTS: dict[str, Any] = {
    "candidate_set": "locked_existing_44",
    "expected_candidate_count": EXPECTED_CANDIDATE_COUNT,
    "network_access": False,
    "retrieval_performed": False,
    "api_submission": False,
    "human_review_decision": False,
    "approval_performed": False,
    "bootstrap_content_fallback": False,
    "candidate_embedded_content_allowed": False,
    "default_extraction_permission": ExtractionPermission.NONE.value,
    "default_storage_permission": StoragePermission.NONE.value,
    "default_sensitivity": SensitiveInformationLevel.CONTROLLED.value,
    "registration_citation_verification": [],
}

_INPUT_FILES: tuple[tuple[str, str, str], ...] = (
    (
        "academic_candidates",
        "candidates/academic-candidates.json",
        SourceCategory.ACADEMIC.value,
    ),
    (
        "practitioner_candidates",
        "candidates/practitioner-candidates.json",
        SourceCategory.PRACTITIONER.value,
    ),
    (
        "verified_academic_sources",
        "sources/verified-academic-sources.json",
        SourceCategory.ACADEMIC.value,
    ),
    (
        "access_checked_practitioner_sources",
        "sources/access-checked-practitioner-sources.json",
        SourceCategory.PRACTITIONER.value,
    ),
)

# This lock prevents a different set of 44 records from being substituted for
# Corpus Run 001 while still satisfying a count-only check.
EXPECTED_CANDIDATE_IDS = frozenset(
    {
        "06f8aa8e-c503-5a05-8dcf-c8dcd7df0143",
        "0aa3498c-545e-54d0-8821-a3399f69cadb",
        "0b5f51cf-9ee8-5925-ac3e-cec8442589aa",
        "10f7bd2c-a1e3-5370-997c-610b6310c5fe",
        "113951a5-c5e3-5c0b-b3a5-67926c1684c9",
        "143fcac3-dbec-57ce-8c81-31671102fda0",
        "1c486620-98b5-564e-a1c3-a55a8e80dda0",
        "26cce342-8924-57f0-91f7-3a7c9ca76698",
        "282bfa90-a30b-5604-9dd5-a046cee0b2fe",
        "30021afa-83ba-5ff0-86ae-df6fb81a2e2d",
        "3fea4cc9-2ddd-5850-8876-98214d2137a6",
        "4347cb46-1849-5ec5-95f0-6575297bc1f6",
        "502353e7-6c17-5e7f-8cff-c1eb3acdcf67",
        "56ec51b3-ae59-50d5-9240-9c1afb9f1df1",
        "59402307-574e-5274-a71e-e13631e8d825",
        "6102b192-dc09-54aa-8749-10b51bc36a5c",
        "6688fca6-8268-5b49-8e7d-8fcb7f74a840",
        "673af584-313b-57e2-96c6-2db14f6ca790",
        "6bca6820-339b-5e20-b24f-4d67ee76ae8e",
        "785ecaa9-2ad0-57d5-a1d4-f0e541bb285a",
        "7e449527-3cba-5275-a040-271080676e18",
        "8ab6c492-362c-598d-9bc8-cf94cbdea335",
        "918d4b55-3aee-593b-a727-047fc4c2d929",
        "91b5290e-e46f-5896-bc1e-3671a4234743",
        "95ea3bfb-3713-58ea-b5c3-661198f38b10",
        "973794a6-afcc-5a10-825f-f4ad77c2c294",
        "97435eeb-34b0-5832-bd09-95708c653d09",
        "9d7ca565-9c3a-584c-b920-6a2a6a26d085",
        "a07202c4-9e79-5fc7-a35a-0a7cc174e897",
        "a2eced72-3171-5390-a391-5d64163b77e0",
        "a741bb54-468e-584c-9b3e-e8b5004d2436",
        "b74289cb-04f0-5148-bc06-ef599c89ac47",
        "b99da09e-bbcd-5058-9744-219242d0a41c",
        "bf3470f2-0d4e-524b-9729-245890154543",
        "c5d7e8d5-f363-5aec-99c1-4198bef3ca61",
        "ce585c31-5cff-5378-922c-23792148a5a2",
        "d3915153-fd57-5803-8d06-6a61d0059d59",
        "d3db9066-f3a4-5535-9104-111a299dc06e",
        "d7ea7887-d2dd-5db2-b777-91a2b1e78982",
        "ed75f8b7-0380-5a5d-9807-829b0b7deaf2",
        "ef17e8c8-d91a-55f1-b0ab-6ff674ed3c38",
        "ef433a85-5738-52e0-b467-42396f9df621",
        "f4c545ea-87b3-5f99-9e1f-bdd14948cd3a",
        "fd00e661-4413-587a-847d-2d1f51d6c5d9",
    }
)

_READY_ACCESS_STATES = {
    "full_text_access_confirmed",
    "web_extract_access_confirmed",
}

_PRACTITIONER_SOURCE_TYPES: dict[str, SourceType | None] = {
    "primary_practitioner_interview": SourceType.INTERVIEW,
    "primary_practitioner_essay": SourceType.WEB_ARTICLE,
    "expert_book_review": SourceType.WEB_ARTICLE,
    "practitioner_essay": SourceType.WEB_ARTICLE,
    "secondary_practitioner_analysis": SourceType.WEB_ARTICLE,
    "primary_practitioner_interview_transcript": SourceType.TRANSCRIPT,
    # The page is an unofficial archive, and the run does not establish
    # whether the governed Source should be typed as interview or transcript.
    "unofficial_interview_archive": None,
    "institutional_historical_interview": SourceType.INTERVIEW,
    "commercial_practitioner_essay": SourceType.WEB_ARTICLE,
    "secondary_book_discussion": SourceType.WEB_ARTICLE,
}


class AcquisitionStagingError(ValueError):
    """Stable fail-closed error raised at the staging trust boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExactContentReceipt(BaseModel):
    """Sidecar attestation for one independently acquired exact text file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["exact-source-content-receipt-0.1"] = (
        RECEIPT_SCHEMA_VERSION
    )
    candidate_id: UUID
    content_file: str = Field(min_length=1)
    content_access: ContentAccess
    source_locator: str = Field(min_length=1, max_length=4_000)
    acquisition_method: str = Field(min_length=1, max_length=300)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_bytes: int = Field(ge=1)
    access: SourceAccessMetadata
    provider: Literal["tavily"] | None = None
    provider_request_id: str | None = Field(default=None, min_length=1, max_length=300)
    extract_depth: Literal["basic", "advanced"] | None = None
    provider_result_title: str | None = Field(default=None, max_length=2_000)
    provider_response_saved_at: datetime
    returned_url: str = Field(min_length=1, max_length=4_000)
    validation: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content_access")
    @classmethod
    def require_exact_content_channel(cls, value: ContentAccess) -> ContentAccess:
        if value not in {
            ContentAccess.FULL_TEXT,
            ContentAccess.PDF_TEXT,
            ContentAccess.WEB_EXTRACT,
        }:
            raise ValueError("receipt content_access must represent exact source text")
        return value

    @field_validator("provider_response_saved_at")
    @classmethod
    def require_aware_saved_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provider_response_saved_at must include a timezone")
        return value


def build_acquisition_manifest(source_run_dir: Path) -> dict[str, Any]:
    """Return a deterministic, content-free manifest for the locked 44 records."""

    source_run_dir = Path(source_run_dir)
    raw_inputs: dict[str, list[dict[str, Any]]] = {}
    input_records: list[dict[str, Any]] = []
    for role, relative_path, _category in _INPUT_FILES:
        path = source_run_dir / relative_path
        raw = _read_required_json_list(path)
        raw_inputs[role] = raw
        input_records.append(
            {
                "role": role,
                "path": relative_path,
                "sha256": _sha256_bytes(path.read_bytes()),
                "record_count": len(raw),
            }
        )

    candidates = _load_locked_candidates(raw_inputs)
    sources = _load_source_records(raw_inputs)
    if set(candidates) != set(sources):
        raise AcquisitionStagingError(
            "candidate_source_set_mismatch",
            "Candidate and verified/access-checked Source record sets differ.",
        )
    if input_records != [dict(value) for value in EXPECTED_INPUT_LOCK]:
        raise AcquisitionStagingError(
            "source_run_input_lock_mismatch",
            "Corpus Run 001 input bytes do not match the locked four-file set.",
        )

    entries = [
        _build_entry(candidates[candidate_id], sources[candidate_id])
        for candidate_id in sorted(candidates)
    ]
    category_counts = {
        category.value: sum(
            entry["source_category"] == category.value for entry in entries
        )
        for category in (SourceCategory.ACADEMIC, SourceCategory.PRACTITIONER)
    }
    summary = {
        "candidate_count": len(entries),
        "category_counts": category_counts,
        "acquisition_ready": sum(
            entry["acquisition"]["status"] == "ready" for entry in entries
        ),
        "access_unresolved": sum(
            entry["acquisition"]["status"] == "access_unresolved"
            for entry in entries
        ),
        "exact_content_missing": len(entries),
        "production_payload_ready": 0,
        "manual_source_type_required": sum(
            entry["classification"]["requires_human_source_type"]
            for entry in entries
        ),
    }
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": ACQUISITION_RUN_ID,
        "source_run_id": SOURCE_RUN_ID,
        "constraints": dict(EXPECTED_MANIFEST_CONSTRAINTS),
        "inputs": input_records,
        "summary": summary,
        "entries": entries,
    }
    manifest["manifest_sha256"] = _canonical_json_hash(manifest)
    return manifest


def write_acquisition_manifest(source_run_dir: Path, output_path: Path) -> dict[str, Any]:
    """Build and atomically write a byte-stable manifest JSON document."""

    manifest = build_acquisition_manifest(source_run_dir)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = _pretty_json(manifest)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(output_path)
    return manifest


def build_production_submission(
    manifest_entry: dict[str, Any],
    receipt: ExactContentReceipt | dict[str, Any] | None,
    staging_root: Path,
) -> dict[str, Any]:
    """Compile one offline Production request only from attested exact content.

    This function does not submit the request.  It rejects absent/blank text,
    unsafe paths, checksum drift, unknown Source types, and locators outside the
    manifest.  It deliberately emits no approval or review decision.
    """

    candidate_id, parsed_receipt = _validate_receipt_identity(manifest_entry, receipt)
    exact_content = manifest_entry.get("exact_content") or {}
    expected_path = str(exact_content.get("content_file") or "")
    content_path = _resolve_exact_content_path(
        Path(staging_root), expected_path, candidate_id
    )
    if not content_path.is_file():
        raise AcquisitionStagingError(
            "exact_content_missing", f"Exact content file is missing for {candidate_id}."
        )
    return build_production_submission_from_content(
        manifest_entry, parsed_receipt, content_path.read_bytes()
    )


def build_production_submission_from_content(
    manifest_entry: dict[str, Any],
    receipt: ExactContentReceipt | dict[str, Any] | None,
    content_bytes: bytes,
) -> dict[str, Any]:
    """Compile a SourceVersion wrapper entirely in memory.

    This is the write-before-commit variant used by acquisition processors: all
    payload bytes can be serialized and hashed before the first filesystem
    mutation.  It performs the same receipt, locator, and content checks as the
    file-backed helper above.
    """

    candidate_id, parsed_receipt = _validate_receipt_identity(manifest_entry, receipt)
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AcquisitionStagingError(
            "exact_content_not_utf8", "Exact content must be UTF-8 text."
        ) from exc
    if not content.strip():
        raise AcquisitionStagingError(
            "exact_content_empty",
            "Blank exact content cannot create a Production payload.",
        )
    content_hash = _sha256_bytes(content_bytes)
    if parsed_receipt.content_bytes != len(content_bytes):
        raise AcquisitionStagingError(
            "content_length_mismatch", "Receipt byte length does not match content."
        )
    if parsed_receipt.content_sha256 != content_hash:
        raise AcquisitionStagingError(
            "content_hash_mismatch", "Receipt hash does not match exact content."
        )
    _require_known_locator(manifest_entry, parsed_receipt.source_locator)
    _require_known_locator(manifest_entry, parsed_receipt.returned_url)
    if _canonical_url(parsed_receipt.returned_url) != _canonical_url(
        parsed_receipt.source_locator
    ):
        raise AcquisitionStagingError(
            "returned_url_mismatch",
            "Receipt returned URL must match its exact acquisition locator.",
        )

    source_type = (manifest_entry.get("classification") or {}).get("source_type")
    if not source_type:
        raise AcquisitionStagingError(
            "source_type_human_choice_required",
            "The Source type is unresolved and must be selected before compilation.",
        )

    payload = {
        "schema_version": "source-workflow-0.1",
        "citation": manifest_entry["citation"],
        "source_category": manifest_entry["source_category"],
        "source_type": source_type,
        "knowledge_origin": manifest_entry["classification"]["knowledge_origin"],
        "content": content,
        "content_access": parsed_receipt.content_access.value,
        "access": parsed_receipt.access.model_dump(mode="json"),
        "requested_extraction_permission": ExtractionPermission.NONE.value,
        "requested_storage_permission": StoragePermission.NONE.value,
        "requested_scope_locators": [],
        "sensitive_information_level": SensitiveInformationLevel.CONTROLLED.value,
        "citation_verification": [],
    }
    command = SourceVersionCommand.model_validate(payload)
    normalized_payload = command.model_dump(mode="json")
    return {
        "candidate_id": candidate_id,
        "expected_production_source_id": manifest_entry[
            "expected_production_source_id"
        ],
        "content_sha256": content_hash,
        "idempotency_key": (
            f"corpus-run-001:source:{candidate_id}:{content_hash}"
        ),
        "payload": normalized_payload,
    }


def _validate_receipt_identity(
    manifest_entry: dict[str, Any],
    receipt: ExactContentReceipt | dict[str, Any] | None,
) -> tuple[str, ExactContentReceipt]:
    candidate_id = str(manifest_entry.get("candidate_id") or "")
    if not candidate_id:
        raise AcquisitionStagingError(
            "manifest_candidate_id_missing", "Manifest entry has no candidate ID."
        )
    if receipt is None:
        raise AcquisitionStagingError(
            "exact_content_receipt_required",
            f"Exact content receipt is required for {candidate_id}.",
        )
    parsed_receipt = (
        receipt
        if isinstance(receipt, ExactContentReceipt)
        else ExactContentReceipt.model_validate(receipt)
    )
    if str(parsed_receipt.candidate_id) != candidate_id:
        raise AcquisitionStagingError(
            "receipt_candidate_mismatch",
            "Exact content receipt belongs to a different candidate.",
        )
    expected_path = str(
        (manifest_entry.get("exact_content") or {}).get("content_file") or ""
    )
    if parsed_receipt.content_file != expected_path:
        raise AcquisitionStagingError(
            "unexpected_content_path",
            "Receipt content path does not match the manifest content slot.",
        )
    return candidate_id, parsed_receipt


def _load_locked_candidates(
    raw_inputs: dict[str, list[dict[str, Any]]],
) -> dict[str, ResearchCandidate]:
    candidates: dict[str, ResearchCandidate] = {}
    for role, _path, expected_category in _INPUT_FILES[:2]:
        for raw in raw_inputs[role]:
            # Discovery snippets/content are not immutable Source content.  A
            # non-empty value is rejected rather than copied or silently ignored.
            if str(raw.get("content") or "").strip() or str(
                raw.get("snippet") or ""
            ).strip():
                raise AcquisitionStagingError(
                    "candidate_embedded_content_forbidden",
                    "Discovery candidate content/snippets cannot seed exact Source content.",
                )
            candidate = ResearchCandidate.model_validate(raw)
            if candidate.source_category.value != expected_category:
                raise AcquisitionStagingError(
                    "candidate_category_mismatch",
                    f"Candidate {candidate.id} is in the wrong input file.",
                )
            if candidate.id in candidates:
                raise AcquisitionStagingError(
                    "duplicate_candidate_id", f"Duplicate candidate ID {candidate.id}."
                )
            candidates[candidate.id] = candidate

    actual_ids = frozenset(candidates)
    if actual_ids != EXPECTED_CANDIDATE_IDS:
        raise AcquisitionStagingError(
            "locked_candidate_set_mismatch",
            "Inputs do not contain the exact locked Corpus Run 001 candidate set.",
        )
    if len(candidates) != EXPECTED_CANDIDATE_COUNT:
        raise AcquisitionStagingError(
            "candidate_count_mismatch",
            f"Expected {EXPECTED_CANDIDATE_COUNT} candidates.",
        )
    return candidates


def _load_source_records(
    raw_inputs: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for role, _path, expected_category in _INPUT_FILES[2:]:
        for raw in raw_inputs[role]:
            candidate_id = str(raw.get("candidate_id") or "")
            try:
                UUID(candidate_id)
            except ValueError as exc:
                raise AcquisitionStagingError(
                    "invalid_source_candidate_id",
                    "A Source record has an invalid candidate ID.",
                ) from exc
            if raw.get("source_category") != expected_category:
                raise AcquisitionStagingError(
                    "source_category_mismatch",
                    f"Source record {candidate_id} is in the wrong input file.",
                )
            if candidate_id in sources:
                raise AcquisitionStagingError(
                    "duplicate_source_candidate_id",
                    f"Duplicate Source record for {candidate_id}.",
                )
            approval = raw.get("source_approval") or {}
            if approval.get("status") != "pending":
                raise AcquisitionStagingError(
                    "unexpected_legacy_approval_state",
                    "Run 001 Source records must remain pending and cannot seed approval.",
                )
            sources[candidate_id] = raw
    return sources


def _build_entry(
    candidate: ResearchCandidate, source_record: dict[str, Any]
) -> dict[str, Any]:
    candidate_id = candidate.id
    source_citation = dict(source_record.get("citation") or {})
    _validate_candidate_alignment(candidate, source_citation)

    citation = CitationMetadataInput.model_validate(
        {
            "title": source_citation.get("title"),
            "authors": source_citation.get("authors") or [],
            "year": source_citation.get("year"),
            "venue": source_citation.get("venue") or "",
            "volume": source_citation.get("volume") or "",
            "issue": source_citation.get("issue") or "",
            "pages": source_citation.get("pages") or "",
            "doi": source_citation.get("doi"),
            "url": source_citation.get("url"),
            "peer_review_status": source_citation.get("peer_review_status")
            or "unknown",
            "provenance": source_citation.get("provenance") or [],
        }
    )
    identity = _source_identity(citation)
    if identity["expected_production_source_id"] != candidate_id:
        raise AcquisitionStagingError(
            "candidate_identity_mismatch",
            f"Candidate {candidate_id} does not match its DOI/URL identity.",
        )

    source_role = (
        "journal_article"
        if candidate.source_category == SourceCategory.ACADEMIC
        else str(source_record.get("source_role") or "")
    )
    if candidate.source_category == SourceCategory.ACADEMIC:
        source_type: SourceType | None = SourceType.JOURNAL_ARTICLE
        knowledge_origin = KnowledgeOrigin.SCIENTIFIC_EVIDENCE
    else:
        if source_role not in _PRACTITIONER_SOURCE_TYPES:
            raise AcquisitionStagingError(
                "unknown_practitioner_source_role",
                f"No conservative Source type mapping exists for {source_role!r}.",
            )
        source_type = _PRACTITIONER_SOURCE_TYPES[source_role]
        knowledge_origin = KnowledgeOrigin.EXPERT_PRACTICE

    access_check = dict(source_record.get("access_check") or {})
    access_state = str(access_check.get("state") or "")
    content_locators = _content_locators(candidate, source_record, access_state)
    acquisition_status = (
        "ready" if access_state in _READY_ACCESS_STATES else "access_unresolved"
    )
    blocked_reasons = ["exact_content_missing"]
    if acquisition_status != "ready":
        blocked_reasons.append("content_access_unresolved")
    if source_type is None:
        blocked_reasons.append("source_type_human_choice_required")

    return {
        "candidate_id": candidate_id,
        "expected_production_source_id": identity[
            "expected_production_source_id"
        ],
        "legacy_discovery_source_record_id": source_record.get("source_id"),
        "source_category": candidate.source_category.value,
        "identity": {
            "kind": identity["kind"],
            "canonical_value": identity["canonical_value"],
            "canonical_key": identity["canonical_key"],
        },
        "citation": citation.model_dump(mode="json"),
        "classification": {
            "source_role": source_role,
            "source_type": source_type.value if source_type else None,
            "requires_human_source_type": source_type is None,
            "knowledge_origin": knowledge_origin.value,
            "classification_is_approval": False,
        },
        "acquisition": {
            "status": acquisition_status,
            "access_state": access_state,
            "checked_at": access_check.get("checked_at"),
            "notes": access_check.get("notes") or "",
            "content_locators": content_locators,
        },
        "exact_content": {
            "status": "missing",
            "content_file": f"content/{candidate_id}.txt",
            "receipt_file": f"receipts/{candidate_id}.json",
            "encoding": "utf-8",
            "content_access": None,
            "content_sha256": None,
            "content_bytes": None,
        },
        "production": {
            "payload_eligible": False,
            "blocked_reasons": blocked_reasons,
            "payload_file": f"source_payloads/{candidate_id}.json",
            "idempotency_key_template": (
                f"corpus-run-001:source:{candidate_id}:{{content_sha256}}"
            ),
            "requested_extraction_permission": ExtractionPermission.NONE.value,
            "requested_storage_permission": StoragePermission.NONE.value,
            "requested_scope_locators": [],
            "sensitive_information_level": (
                SensitiveInformationLevel.CONTROLLED.value
            ),
            "citation_verification": [],
        },
    }


def _content_locators(
    candidate: ResearchCandidate,
    source_record: dict[str, Any],
    access_state: str,
) -> list[dict[str, Any]]:
    locators: list[dict[str, Any]] = []
    checked_url = str((source_record.get("access_check") or {}).get("url") or "")
    if checked_url and access_state in _READY_ACCESS_STATES:
        locators.append(
            {
                "kind": (
                    "publisher_or_repository_html"
                    if candidate.source_category == SourceCategory.ACADEMIC
                    else "practitioner_web_page"
                ),
                "url": checked_url,
                "content_access_hint": (
                    ContentAccess.WEB_EXTRACT.value
                ),
                "license_name": None,
            }
        )
    if candidate.source_category == SourceCategory.ACADEMIC:
        open_pdf = dict(
            ((source_record.get("semantic_scholar") or {}).get("openAccessPdf") or {})
        )
        pdf_url = str(open_pdf.get("url") or "")
        if pdf_url and access_state in _READY_ACCESS_STATES:
            locators.append(
                {
                    "kind": "open_access_pdf_candidate",
                    "url": pdf_url,
                    "content_access_hint": ContentAccess.PDF_TEXT.value,
                    "license_name": open_pdf.get("license"),
                }
            )
    return _deduplicate_locators(locators)


def _validate_candidate_alignment(
    candidate: ResearchCandidate, citation: dict[str, Any]
) -> None:
    comparisons = {
        "title": (candidate.title, citation.get("title")),
        "authors": (candidate.authors, citation.get("authors") or []),
        "year": (candidate.published_year, citation.get("year")),
        "venue": (candidate.venue, citation.get("venue") or ""),
        "doi": (_normalize_doi(candidate.doi), _normalize_doi(citation.get("doi"))),
        "url": (_canonical_url(candidate.url), _canonical_url(citation.get("url"))),
    }
    mismatches = [name for name, values in comparisons.items() if values[0] != values[1]]
    if mismatches:
        raise AcquisitionStagingError(
            "candidate_source_metadata_mismatch",
            f"Candidate {candidate.id} differs from its Source record: {', '.join(mismatches)}.",
        )


def _source_identity(citation: CitationMetadataInput) -> dict[str, str]:
    if citation.doi:
        identity = _normalize_doi(citation.doi)
        assert identity is not None
        return {
            "kind": "doi",
            "canonical_value": identity,
            "canonical_key": f"doi:{identity}",
            "expected_production_source_id": str(
                uuid5(NAMESPACE_URL, f"magicforge:research:{identity}")
            ),
        }
    canonical_url = _canonical_url(citation.url)
    digest = _sha256_text(canonical_url)
    return {
        "kind": "url",
        "canonical_value": canonical_url,
        "canonical_key": f"url-sha256:{digest}",
        "expected_production_source_id": str(
            uuid5(NAMESPACE_URL, f"magicforge:research:{canonical_url}")
        ),
    }


def _resolve_exact_content_path(
    staging_root: Path, relative_path: str, candidate_id: str
) -> Path:
    required = f"content/{candidate_id}.txt"
    if relative_path != required:
        raise AcquisitionStagingError(
            "unsafe_content_path",
            "Exact content must use its dedicated acquisition content slot.",
        )
    root = staging_root.resolve()
    resolved = (root / relative_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AcquisitionStagingError(
            "unsafe_content_path", "Exact content path escapes the staging root."
        ) from exc
    return resolved


def _require_known_locator(entry: dict[str, Any], locator: str) -> None:
    known = {
        _canonical_url(str(item.get("url") or ""))
        for item in (entry.get("acquisition") or {}).get("content_locators", [])
        if item.get("url")
    }
    if _canonical_url(locator) not in known:
        raise AcquisitionStagingError(
            "unknown_content_locator",
            "Receipt locator is not an approved acquisition locator in the manifest.",
        )


def _read_required_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise AcquisitionStagingError(
            "required_input_missing", f"Required input is missing: {path}"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcquisitionStagingError(
            "invalid_input_json", f"Could not read JSON input: {path}"
        ) from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise AcquisitionStagingError(
            "invalid_input_shape", f"Input must be a JSON object array: {path}"
        )
    return value


def _normalize_doi(value: object) -> str | None:
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
    return normalized


def _canonical_url(value: object) -> str:
    normalized = str(value or "").strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise AcquisitionStagingError(
            "invalid_source_url", "Source/acquisition URL is invalid."
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


def _deduplicate_locators(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for item in values:
        unique.setdefault(_canonical_url(item["url"]), item)
    return [unique[key] for key in sorted(unique)]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _canonical_json_hash(value: dict[str, Any]) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _sha256_text(serialized)


def _pretty_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    ) + "\n"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build an offline Source-acquisition manifest for the locked "
            "MagicForge Corpus Run 001 candidate set."
        )
    )
    parser.add_argument(
        "--source-run",
        type=Path,
        default=Path("research/runs/magicforge-corpus-run-001"),
        help="Directory containing the four Run 001 JSON inputs.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "research/runs/production-source-acquisition-001/"
            "acquisition-manifest.json"
        ),
        help="Offline manifest output path.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate and summarize without writing a file.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        manifest = (
            build_acquisition_manifest(args.source_run)
            if args.check
            else write_acquisition_manifest(args.source_run, args.output)
        )
    except AcquisitionStagingError as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": str(exc)}))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "written": None if args.check else str(args.output),
                "manifest_sha256": manifest["manifest_sha256"],
                **manifest["summary"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ACQUISITION_RUN_ID",
    "EXPECTED_CANDIDATE_IDS",
    "EXPECTED_INPUT_LOCK",
    "EXPECTED_MANIFEST_CONSTRAINTS",
    "AcquisitionStagingError",
    "ExactContentReceipt",
    "build_acquisition_manifest",
    "build_production_submission",
    "build_production_submission_from_content",
    "main",
    "write_acquisition_manifest",
]
