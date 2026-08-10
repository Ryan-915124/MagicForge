"""Generic, offline Source-acquisition staging for future corpus runs.

The legacy Corpus Run 001 staging code intentionally locks one historical
batch.  This module provides the reusable boundary for later runs without
weakening that lock.  It consumes already-saved, normalized discovery records
and an already-saved exact-content bundle; it performs no network request,
database write, approval, semantic extraction, or vector projection.

Every emitted ledger and Source payload is content addressed.  The CLI is
always a dry run: there is deliberately no submit flag.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from knowledge.evidence import KnowledgeOrigin, SourceType
from knowledge.governance import (
    ExtractionPermission,
    SensitiveInformationLevel,
    StoragePermission,
)
from research.citation.models import PeerReviewStatus
from research.models import (
    ContentAccess,
    DiscoveryProvider,
    ResearchCandidate,
    SearchProvenance,
    SourceCategory,
)
from research.review.workflow_models import SourceVersionCommand

from .staging import AcquisitionStagingError


LEDGER_SCHEMA_VERSION = "generic-source-acquisition-ledger-0.1"
REPORT_SCHEMA_VERSION = "generic-source-acquisition-dry-run-0.1"
_DOI = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_EXACT_CONTENT_CHANNELS = {
    ContentAccess.WEB_EXTRACT,
    ContentAccess.FULL_TEXT,
    ContentAccess.PDF_TEXT,
}


@dataclass
class _CandidateState:
    candidate: ResearchCandidate
    source_type: SourceType | None
    knowledge_origin: KnowledgeOrigin
    sensitivity: SensitiveInformationLevel
    peer_review_status: PeerReviewStatus
    volume: str = ""
    issue: str = ""
    pages: str = ""
    input_records: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _ExactContent:
    candidate_id: str | None
    requested_url: str
    returned_url: str
    title: str
    content: str
    content_access: ContentAccess
    provider: str
    retrieved_at: datetime
    access: dict[str, Any]
    redirect_verified: bool


def stage_generic_acquisition(
    discovery_paths: Sequence[Path],
    content_bundle_path: Path,
    output_dir: Path,
    *,
    existing_candidate_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    """Build an immutable dry-run ledger and safe Source submission payloads.

    ``discovery_paths`` accept either a list of normalized result objects or an
    object with a ``results`` list.  ``content_bundle_path`` accepts the
    Bootstrap ``{"sources": {url: record}}`` shape and a generic
    ``{"sources": [record, ...]}`` shape.
    """

    if not discovery_paths:
        raise AcquisitionStagingError(
            "discovery_input_required", "At least one discovery input is required."
        )
    discovery_inputs, states, duplicate_rows = _load_discovery(discovery_paths)
    existing_keys = _load_existing_keys(existing_candidate_paths)
    retained: list[_CandidateState] = []
    duplicate_existing: list[dict[str, Any]] = []
    for state in sorted(states, key=lambda value: value.candidate.id):
        overlap = sorted(_identity_keys(state.candidate) & existing_keys)
        if overlap:
            duplicate_existing.append(
                {
                    "candidate_id": state.candidate.id,
                    "title": state.candidate.title,
                    "matched_identity": overlap[0],
                }
            )
            continue
        retained.append(state)

    content_input, content_items = _load_content_bundle(Path(content_bundle_path))
    content_matches = _match_content(retained, content_items)
    entries: list[dict[str, Any]] = []
    payload_artifacts: list[tuple[Path, bytes]] = []
    for state in retained:
        exact = content_matches.get(state.candidate.id)
        entry, artifact = _build_entry(state, exact)
        entries.append(entry)
        if artifact is not None:
            relative_path, payload_bytes = artifact
            entry["source_submission"]["payload_file"] = relative_path.as_posix()
            entry["source_submission"]["payload_sha256"] = _sha256_bytes(
                payload_bytes
            )
            payload_artifacts.append((relative_path, payload_bytes))

    ledger_without_hash: dict[str, Any] = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "mode": "dry_run",
        "constraints": {
            "network_access": False,
            "database_write": False,
            "api_submission": False,
            "approval_performed": False,
            "human_review_decision": False,
            "semantic_extraction": False,
            "qdrant_write": False,
            "candidate_snippet_is_source_content": False,
            "requested_extraction_permission": ExtractionPermission.NONE.value,
            "requested_storage_permission": StoragePermission.NONE.value,
        },
        "inputs": [*discovery_inputs, content_input],
        "existing_candidate_inputs": [
            _input_commitment(Path(path), "existing_candidates")
            for path in existing_candidate_paths
        ],
        "deduplication": {
            "duplicate_discovery_rows": duplicate_rows,
            "duplicates_against_existing": duplicate_existing,
        },
        "summary": {
            "discovery_candidates": len(states),
            "retained_candidates": len(retained),
            "exact_content_matched": sum(
                entry["exact_content"]["status"] == "matched" for entry in entries
            ),
            "source_payloads_prepared": sum(
                entry["source_submission"]["status"] == "prepared"
                for entry in entries
            ),
            "blocked_candidates": sum(
                entry["source_submission"]["status"] == "blocked"
                for entry in entries
            ),
        },
        "entries": entries,
    }
    ledger_hash = _canonical_hash(ledger_without_hash)
    ledger = {**ledger_without_hash, "ledger_sha256": ledger_hash}

    output_dir = Path(output_dir)
    for relative_path, payload_bytes in payload_artifacts:
        _write_immutable(output_dir / relative_path, payload_bytes)
    ledger_path = Path("ledgers") / f"{ledger_hash}.json"
    absolute_ledger_path = output_dir / ledger_path
    ledger_was_new = not absolute_ledger_path.exists()
    previous_ledger_mtime = _latest_ledger_mtime(output_dir / "ledgers")
    _write_immutable(absolute_ledger_path, _json_bytes(ledger))
    if ledger_was_new:
        _order_new_ledger_after(
            absolute_ledger_path,
            previous_mtime_ns=previous_ledger_mtime,
        )

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "dry_run",
        "ledger_sha256": ledger_hash,
        "ledger_file": ledger_path.as_posix(),
        "summary": ledger["summary"],
        "safety": {
            "network_called": False,
            "database_modified": False,
            "approval_created": False,
            "qdrant_modified": False,
        },
    }
    report_path = Path("reports") / f"{ledger_hash}.json"
    _write_immutable(output_dir / report_path, _json_bytes(report))
    report["report_file"] = report_path.as_posix()
    return report


def verify_generic_staging(output_dir: Path, ledger_sha256: str) -> dict[str, int]:
    """Verify the immutable ledger and all payload byte commitments."""

    if not re.fullmatch(r"[0-9a-f]{64}", ledger_sha256):
        raise AcquisitionStagingError("invalid_ledger_hash", "Invalid ledger hash.")
    output_dir = Path(output_dir)
    ledger_path = output_dir / "ledgers" / f"{ledger_sha256}.json"
    ledger_bytes = _read_required_bytes(ledger_path, "ledger_missing")
    ledger = _read_json_object_bytes(ledger_bytes, "invalid_ledger")
    recorded = str(ledger.pop("ledger_sha256", ""))
    if recorded != ledger_sha256 or _canonical_hash(ledger) != ledger_sha256:
        raise AcquisitionStagingError(
            "ledger_hash_mismatch", "Ledger bytes do not match their address."
        )
    verified = 0
    for entry in ledger.get("entries", []):
        submission = entry.get("source_submission") or {}
        if submission.get("status") != "prepared":
            continue
        relative = str(submission.get("payload_file") or "")
        path = _safe_relative_output(output_dir, relative, "source_payloads")
        payload = _read_required_bytes(path, "source_payload_missing")
        if _sha256_bytes(payload) != submission.get("payload_sha256"):
            raise AcquisitionStagingError(
                "source_payload_hash_mismatch",
                f"Source payload checksum mismatch for {entry.get('candidate_id')}",
            )
        wrapper = _read_json_object_bytes(payload, "invalid_source_payload")
        SourceVersionCommand.model_validate(wrapper["payload"])
        verified += 1
    return {"ledger_verified": 1, "source_payloads_verified": verified}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline generic acquisition staging (always dry-run)."
    )
    parser.add_argument("--discovery", type=Path, action="append", required=True)
    parser.add_argument("--content-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--existing-candidates", type=Path, action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = stage_generic_acquisition(
        args.discovery,
        args.content_bundle,
        args.output_dir,
        existing_candidate_paths=args.existing_candidates,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(
            "Dry run complete: "
            f"{summary['retained_candidates']} candidates, "
            f"{summary['source_payloads_prepared']} safe Source payloads, "
            f"ledger {report['ledger_sha256']}."
        )
    return 0


def _load_discovery(
    paths: Sequence[Path],
) -> tuple[list[dict[str, Any]], list[_CandidateState], int]:
    inputs: list[dict[str, Any]] = []
    states: list[_CandidateState] = []
    duplicate_rows = 0
    for input_index, path_value in enumerate(paths):
        path = Path(path_value)
        rows = _read_records(path, "discovery")
        inputs.append(_input_commitment(path, "discovery", len(rows)))
        for row_index, row in enumerate(rows):
            state = _state_from_row(row, f"discovery:{input_index}:{row_index}")
            match = next(
                (
                    existing
                    for existing in states
                    if _identity_keys(existing.candidate)
                    & _identity_keys(state.candidate)
                ),
                None,
            )
            if match is None:
                states.append(state)
            else:
                duplicate_rows += 1
                _merge_state(match, state)
    return inputs, states, duplicate_rows


def _state_from_row(row: dict[str, Any], input_record: str) -> _CandidateState:
    if str(row.get("content") or "").strip():
        raise AcquisitionStagingError(
            "discovery_content_forbidden",
            "Discovery records cannot provide exact Source content.",
        )
    title = str(row.get("title") or "").strip()
    url = _canonical_url(str(row.get("url") or ""))
    if not title or not url:
        raise AcquisitionStagingError(
            "invalid_discovery_record", "Discovery rows require title and URL."
        )
    provider = DiscoveryProvider(str(row.get("provider") or ""))
    category = _source_category(row, provider)
    retrieved_at = _aware_datetime(row.get("retrieved_at"), "retrieved_at")
    query = str(row.get("query") or "").strip()
    if not query:
        raise AcquisitionStagingError(
            "discovery_query_missing", "Every discovery row requires its query."
        )
    rank = int(row["rank"]) if row.get("rank") is not None else None
    authors = _authors(row.get("authors", row.get("author")))
    doi = _normalize_doi(row.get("doi")) or _extract_doi(
        f"{url} {row.get('snippet') or ''}"
    )
    candidate = ResearchCandidate(
        title=title,
        url=url,
        authors=authors,
        published_year=_year(row.get("published_year", row.get("published"))),
        venue=str(row.get("venue") or urlsplit(url).netloc).strip(),
        doi=doi,
        snippet=str(row.get("snippet") or "").strip(),
        content=None,
        content_access=ContentAccess.SEARCH_SNIPPET,
        source_category=category,
        provenance=[
            SearchProvenance(
                provider=provider,
                tool_name=str(row.get("tool_name") or _tool_name(provider)),
                query=query,
                retrieved_at=retrieved_at,
                provider_result_id=(
                    str(row["provider_result_id"])
                    if row.get("provider_result_id")
                    else None
                ),
                rank=rank,
            )
        ],
    )
    peer_review = PeerReviewStatus(
        str(row.get("peer_review_status") or PeerReviewStatus.UNKNOWN.value)
    )
    source_type = _source_type(row.get("source_type"), category, peer_review)
    origin = KnowledgeOrigin(
        str(row.get("knowledge_origin") or _default_origin(category).value)
    )
    if category == SourceCategory.PRACTITIONER and origin == KnowledgeOrigin.SCIENTIFIC_EVIDENCE:
        raise AcquisitionStagingError(
            "invalid_knowledge_origin",
            "Practitioner discovery cannot be classified as scientific evidence.",
        )
    return _CandidateState(
        candidate=candidate,
        source_type=source_type,
        knowledge_origin=origin,
        sensitivity=SensitiveInformationLevel(
            str(
                row.get("sensitive_information_level")
                or SensitiveInformationLevel.CONTROLLED.value
            )
        ),
        peer_review_status=peer_review,
        volume=str(row.get("volume") or ""),
        issue=str(row.get("issue") or ""),
        pages=str(row.get("pages") or ""),
        input_records=[input_record],
    )


def _merge_state(target: _CandidateState, incoming: _CandidateState) -> None:
    for field_name in (
        "source_type",
        "knowledge_origin",
        "sensitivity",
        "peer_review_status",
    ):
        current = getattr(target, field_name)
        other = getattr(incoming, field_name)
        if current != other and not (
            field_name == "source_type" and (current is None or other is None)
        ):
            raise AcquisitionStagingError(
                "conflicting_candidate_classification",
                f"Duplicate discovery rows disagree on {field_name}.",
            )
        if field_name == "source_type" and current is None:
            target.source_type = other
    candidate = target.candidate
    provenance = [*candidate.provenance]
    seen = {
        (item.provider, item.query, item.rank, item.provider_result_id)
        for item in provenance
    }
    for item in incoming.candidate.provenance:
        marker = (item.provider, item.query, item.rank, item.provider_result_id)
        if marker not in seen:
            provenance.append(item)
            seen.add(marker)
    target.candidate = candidate.model_copy(
        update={
            "authors": candidate.authors or incoming.candidate.authors,
            "published_year": (
                candidate.published_year or incoming.candidate.published_year
            ),
            "venue": candidate.venue or incoming.candidate.venue,
            "doi": candidate.doi or incoming.candidate.doi,
            "snippet": max(
                (candidate.snippet, incoming.candidate.snippet), key=len
            ),
            "provenance": provenance,
        }
    )
    target.input_records.extend(incoming.input_records)


def _load_existing_keys(paths: Sequence[Path]) -> set[str]:
    keys: set[str] = set()
    for path_value in paths:
        for row in _read_records(Path(path_value), "existing_candidates"):
            try:
                provider = DiscoveryProvider(str(row.get("provider") or "exa"))
                state = _state_from_row(
                    {
                        **row,
                        "provider": provider.value,
                        "query": row.get("query") or "existing-corpus",
                        "retrieved_at": row.get("retrieved_at")
                        or "1970-01-01T00:00:00+00:00",
                    },
                    "existing",
                )
            except (ValueError, AcquisitionStagingError):
                # Historical discovery exports can contain provider redirect
                # placeholders such as ``https:///goto?...``.  They are not
                # valid Source locators and must never be normalized into a
                # new payload, but their DOI/title/author identity is still
                # useful for conservative cross-run deduplication.
                try:
                    candidate = ResearchCandidate.model_validate(row)
                    keys.update(_identity_keys(candidate))
                except (ValueError, AcquisitionStagingError):
                    keys.update(_legacy_existing_identity_keys(row))
            else:
                keys.update(_identity_keys(state.candidate))
    return keys


def _legacy_existing_identity_keys(row: dict[str, Any]) -> set[str]:
    """Return non-locator identities from an invalid historical candidate.

    This helper is intentionally scoped to ``existing_candidate_paths``.  New
    discovery rows still fail closed on malformed URLs.
    """

    keys: set[str] = set()
    doi = _normalize_doi(row.get("doi"))
    if doi:
        keys.add(f"doi:{doi}")
    title = _identity_text(str(row.get("title") or ""))
    authors = _authors(row.get("authors", row.get("author")))
    author = _identity_text(authors[0]) if authors else ""
    if title:
        # Keep a title-only key even when an author is present.  Historical
        # Source records often omit authors while newer discovery records do
        # not; treating those representations as distinct silently admits the
        # same document twice.  This boundary is intentionally conservative.
        keys.add(f"title:{title}")
        if author:
            keys.add(f"title-author:{title}:{author}")
    return keys


def _load_content_bundle(path: Path) -> tuple[dict[str, Any], list[_ExactContent]]:
    raw = _read_json(path)
    values = raw.get("sources") if isinstance(raw, dict) else None
    if isinstance(values, dict):
        rows = []
        for url, value in values.items():
            if not isinstance(value, dict):
                raise AcquisitionStagingError(
                    "invalid_content_bundle", "Content records must be objects."
                )
            rows.append({"requested_url": url, **value})
    elif isinstance(values, list):
        rows = values
    else:
        raise AcquisitionStagingError(
            "invalid_content_bundle", "Content bundle requires a sources object/list."
        )
    items: list[_ExactContent] = []
    for row in rows:
        if not isinstance(row, dict):
            raise AcquisitionStagingError(
                "invalid_content_bundle", "Content records must be objects."
            )
        content = str(row.get("content") or "")
        if not content.strip():
            raise AcquisitionStagingError(
                "exact_content_empty", "Exact content records cannot be blank."
            )
        requested_url = _canonical_url(
            str(row.get("requested_url") or row.get("url") or "")
        )
        returned_url = _canonical_url(
            str(row.get("returned_url") or requested_url)
        )
        if not requested_url or not returned_url:
            raise AcquisitionStagingError(
                "exact_content_url_missing", "Exact content requires URL provenance."
            )
        channel = ContentAccess(
            str(row.get("content_access") or ContentAccess.WEB_EXTRACT.value)
        )
        if channel not in _EXACT_CONTENT_CHANNELS:
            raise AcquisitionStagingError(
                "invalid_exact_content_channel",
                "Discovery snippets and abstracts cannot be exact Source content.",
            )
        provider = str(row.get("provider") or "").strip()
        if not provider:
            raise AcquisitionStagingError(
                "content_provider_missing", "Exact content requires provider provenance."
            )
        title = str(row.get("title") or "").strip()
        if not title:
            raise AcquisitionStagingError(
                "content_title_missing",
                "Exact content requires the returned document title.",
            )
        access = dict(row.get("access") or {})
        access.setdefault("access_method", f"offline_exact_content_bundle:{provider}")
        access.setdefault("license_name", None)
        access.setdefault("rights_uri", None)
        access.setdefault("permission_notes", "Staged for independent Source review.")
        access.setdefault("redistribution_allowed", False)
        items.append(
            _ExactContent(
                candidate_id=(
                    str(row["candidate_id"]) if row.get("candidate_id") else None
                ),
                requested_url=requested_url,
                returned_url=returned_url,
                title=title,
                content=content,
                content_access=channel,
                provider=provider,
                retrieved_at=_aware_datetime(row.get("retrieved_at"), "retrieved_at"),
                access=access,
                redirect_verified=bool(row.get("redirect_verified", False)),
            )
        )
    return _input_commitment(path, "exact_content_bundle", len(items)), items


def _match_content(
    states: Sequence[_CandidateState], items: Sequence[_ExactContent]
) -> dict[str, _ExactContent]:
    matches: dict[str, _ExactContent] = {}
    for state in states:
        candidate = state.candidate
        candidate_url = _canonical_url(candidate.url)
        possible = [
            item
            for item in items
            if item.candidate_id == candidate.id
            or item.requested_url == candidate_url
        ]
        if len(possible) > 1:
            raise AcquisitionStagingError(
                "ambiguous_exact_content",
                f"Multiple exact-content records match {candidate.id}.",
            )
        if possible:
            matches[candidate.id] = possible[0]
    return matches


def _build_entry(
    state: _CandidateState, exact: _ExactContent | None
) -> tuple[dict[str, Any], tuple[Path, bytes] | None]:
    candidate = state.candidate
    identity = _primary_identity(candidate)
    blocked: list[str] = []
    if exact is None:
        blocked.append("exact_content_missing")
    elif exact.requested_url != exact.returned_url and not exact.redirect_verified:
        blocked.append("returned_url_requires_verification")
    if exact is not None and not _titles_align(candidate.title, exact.title):
        blocked.append("returned_title_mismatch")
    if exact is not None and _looks_mojibake(exact.content):
        blocked.append("exact_content_encoding_corrupt")
    if state.source_type is None:
        blocked.append("source_type_requires_review")
    exact_record: dict[str, Any] = {"status": "missing"}
    if exact is not None:
        raw = exact.content.encode("utf-8")
        exact_record = {
            "status": "matched",
            "content_sha256": _sha256_bytes(raw),
            "content_bytes": len(raw),
            "content_access": exact.content_access.value,
            "requested_url": exact.requested_url,
            "returned_url": exact.returned_url,
            "provider": exact.provider,
            "retrieved_at": exact.retrieved_at.isoformat(),
        }
    entry: dict[str, Any] = {
        "candidate_id": candidate.id,
        "identity": identity,
        "canonical_url": _canonical_url(candidate.url),
        "doi": _normalize_doi(candidate.doi),
        "title": candidate.title,
        "authors": candidate.authors,
        "published_year": candidate.published_year,
        "venue": candidate.venue,
        "source_category": candidate.source_category.value,
        "source_type": state.source_type.value if state.source_type else None,
        "knowledge_origin": state.knowledge_origin.value,
        "sensitive_information_level": state.sensitivity.value,
        "provenance": [
            item.model_dump(mode="json") for item in candidate.provenance
        ],
        "exact_content": exact_record,
        "source_submission": {
            "status": "blocked" if blocked else "prepared",
            "blocked_reasons": blocked,
            "payload_file": None,
            "payload_sha256": None,
            "requested_extraction_permission": ExtractionPermission.NONE.value,
            "requested_storage_permission": StoragePermission.NONE.value,
            "citation_verification": [],
        },
    }
    if blocked or exact is None or state.source_type is None:
        return entry, None

    payload = SourceVersionCommand.model_validate(
        {
            "citation": {
                "title": candidate.title,
                "authors": candidate.authors,
                "year": candidate.published_year,
                "venue": candidate.venue,
                "volume": state.volume,
                "issue": state.issue,
                "pages": state.pages,
                "doi": candidate.doi,
                "url": _canonical_url(candidate.url),
                "peer_review_status": state.peer_review_status,
                "provenance": [
                    item.model_dump(mode="json") for item in candidate.provenance
                ],
            },
            "source_category": candidate.source_category,
            "source_type": state.source_type,
            "knowledge_origin": state.knowledge_origin,
            "content": exact.content,
            "content_access": exact.content_access,
            "access": exact.access,
            "requested_extraction_permission": ExtractionPermission.NONE,
            "requested_storage_permission": StoragePermission.NONE,
            "requested_scope_locators": [],
            "sensitive_information_level": state.sensitivity,
            "citation_verification": [],
        }
    )
    wrapper = {
        "schema_version": "generic-source-submission-dry-run-0.1",
        "status": "dry_run_not_submitted",
        "candidate_id": candidate.id,
        "identity": identity,
        "content_sha256": exact_record["content_sha256"],
        "idempotency_key": (
            f"generic-source:{candidate.id}:{exact_record['content_sha256']}"
        ),
        "payload": payload.model_dump(mode="json"),
    }
    payload_bytes = _json_bytes(wrapper)
    relative = Path("source_payloads") / candidate.id / f"{_sha256_bytes(payload_bytes)}.json"
    return entry, (relative, payload_bytes)


def _source_category(
    row: dict[str, Any], provider: DiscoveryProvider
) -> SourceCategory:
    explicit = row.get("source_category")
    if explicit:
        return SourceCategory(str(explicit))
    # Compatibility only for the currently normalized discovery exports.  The
    # classification remains explicit in the ledger and may be overridden by
    # adding source_category to future rows.
    return (
        SourceCategory.ACADEMIC
        if provider == DiscoveryProvider.EXA
        else SourceCategory.PRACTITIONER
    )


def _source_type(
    raw: Any, category: SourceCategory, peer_review: PeerReviewStatus
) -> SourceType | None:
    if raw:
        return SourceType(str(raw))
    if category in {SourceCategory.PRACTITIONER, SourceCategory.WEB}:
        return SourceType.WEB_ARTICLE
    if peer_review == PeerReviewStatus.PEER_REVIEWED:
        return SourceType.JOURNAL_ARTICLE
    if peer_review == PeerReviewStatus.PREPRINT:
        return SourceType.PREPRINT
    return None


def _default_origin(category: SourceCategory) -> KnowledgeOrigin:
    if category == SourceCategory.ACADEMIC:
        return KnowledgeOrigin.SCIENTIFIC_EVIDENCE
    if category == SourceCategory.PRACTITIONER:
        return KnowledgeOrigin.EXPERT_PRACTICE
    return KnowledgeOrigin.PERSONAL_INTERPRETATION


def _identity_keys(candidate: ResearchCandidate) -> set[str]:
    keys = {f"url:{_canonical_url(candidate.url)}"}
    doi = _normalize_doi(candidate.doi)
    if doi:
        keys.add(f"doi:{doi}")
    title = _identity_text(candidate.title)
    author = _identity_text(candidate.authors[0]) if candidate.authors else ""
    keys.add(f"title:{title}")
    if author:
        keys.add(f"title-author:{title}:{author}")
    return keys


def _primary_identity(candidate: ResearchCandidate) -> dict[str, str]:
    doi = _normalize_doi(candidate.doi)
    if doi:
        return {"kind": "doi", "canonical_value": doi, "key": f"doi:{doi}"}
    url = _canonical_url(candidate.url)
    return {"kind": "url", "canonical_value": url, "key": f"url:{url}"}


def _read_records(path: Path, role: str) -> list[dict[str, Any]]:
    raw = _read_json(path)
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict) and isinstance(raw.get("results"), list):
        rows = raw["results"]
    elif isinstance(raw, dict) and isinstance(raw.get("candidates"), list):
        rows = raw["candidates"]
    else:
        raise AcquisitionStagingError(
            "invalid_record_collection", f"{role} input must contain a record list."
        )
    if not all(isinstance(row, dict) for row in rows):
        raise AcquisitionStagingError(
            "invalid_record_collection", f"{role} records must be objects."
        )
    return list(rows)


def _input_commitment(path: Path, role: str, count: int | None = None) -> dict[str, Any]:
    raw = _read_required_bytes(path, "input_missing")
    return {
        "role": role,
        "filename": path.name,
        "sha256": _sha256_bytes(raw),
        **({"record_count": count} if count is not None else {}),
    }


def _read_json(path: Path) -> Any:
    raw = _read_required_bytes(path, "input_missing")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcquisitionStagingError(
            "invalid_json", f"Invalid JSON input: {path.name}."
        ) from exc


def _read_json_object_bytes(raw: bytes, code: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcquisitionStagingError(code, "Invalid JSON artifact.") from exc
    if not isinstance(value, dict):
        raise AcquisitionStagingError(code, "JSON artifact must be an object.")
    return value


def _read_required_bytes(path: Path, code: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise AcquisitionStagingError(code, f"Required file unavailable: {path}.") from exc


def _write_immutable(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise AcquisitionStagingError(
            "immutable_artifact_symlink", f"Artifact path is a symlink: {path}."
        )
    if path.exists():
        if path.read_bytes() != value:
            raise AcquisitionStagingError(
                "immutable_artifact_conflict", f"Artifact already exists: {path}."
            )
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(value)
    temporary.chmod(0o600)
    try:
        os.link(temporary, path)
    except FileExistsError:
        if path.read_bytes() != value:
            raise AcquisitionStagingError(
                "immutable_artifact_conflict", f"Artifact already exists: {path}."
            )
    finally:
        temporary.unlink(missing_ok=True)
    path.chmod(0o600)


def _latest_ledger_mtime(directory: Path) -> int:
    """Return the newest immutable ledger timestamp before a new revision."""

    if not directory.exists():
        return 0
    try:
        return max(
            (path.stat().st_mtime_ns for path in directory.glob("*.json")),
            default=0,
        )
    except OSError as exc:
        raise AcquisitionStagingError(
            "ledger_currentness_unavailable",
            "Could not determine existing ledger order.",
        ) from exc


def _order_new_ledger_after(path: Path, *, previous_mtime_ns: int) -> None:
    """Make immutable-history ordering deterministic on coarse filesystems."""

    try:
        stat = path.stat()
        ordered_mtime = max(stat.st_mtime_ns, previous_mtime_ns + 1)
        if ordered_mtime != stat.st_mtime_ns:
            os.utime(path, ns=(stat.st_atime_ns, ordered_mtime))
    except OSError as exc:
        raise AcquisitionStagingError(
            "ledger_currentness_unavailable",
            "Could not establish immutable ledger order.",
        ) from exc


def _safe_relative_output(root: Path, relative: str, prefix: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise AcquisitionStagingError("unsafe_output_path", "Unsafe artifact path.")
    root = root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to((root / prefix).resolve())
    except ValueError as exc:
        raise AcquisitionStagingError("unsafe_output_path", "Unsafe artifact path.") from exc
    return path


def _canonical_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.netloc:
        raise AcquisitionStagingError(
            "invalid_url", "Source URLs must use absolute HTTP(S) locators."
        )
    query = sorted(
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _TRACKING_KEYS
    )
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold().removeprefix("www."),
            parts.path.rstrip("/") or "/",
            urlencode(query),
            "",
        )
    )


def _normalize_doi(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
    match = _DOI.search(normalized)
    return match.group(0).rstrip(".,;") if match else None


def _extract_doi(value: str) -> str | None:
    match = _DOI.search(value)
    return match.group(0).rstrip(".,;").casefold() if match else None


def _identity_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _titles_align(expected: str, returned: str) -> bool:
    expected_tokens = set(re.findall(r"[a-z0-9]{2,}", expected.casefold()))
    returned_tokens = set(re.findall(r"[a-z0-9]{2,}", returned.casefold()))
    if not expected_tokens or not returned_tokens:
        return _identity_text(expected) == _identity_text(returned)
    return len(expected_tokens & returned_tokens) / len(expected_tokens) >= 0.75


def _looks_mojibake(value: str) -> bool:
    """Reject obviously corrupted extracts before they can become Source text.

    A single Latin-1 character can be legitimate.  Repeated UTF-8-as-Latin-1
    marker sequences or any Unicode replacement character indicate that the
    captured bytes are not reliable enough for claim extraction.
    """

    if "\ufffd" in value:
        return True
    markers = ("\u00e2\u20ac", "\u00c3\u201a", "\u00c2\u00a0", "\u00c3\u00a2")
    return sum(value.count(marker) for marker in markers) >= 3


def _authors(value: Any) -> list[str]:
    if isinstance(value, list):
        return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))
    if not value:
        return []
    return [
        item.strip()
        for item in re.split(r"\s*(?:;|\band\b)\s*", str(value))
        if item.strip()
    ]


def _year(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"\b(?:18|19|20|21)\d{2}\b", str(value))
    return int(match.group(0)) if match else None


def _aware_datetime(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        raise AcquisitionStagingError(
            "provenance_timestamp_missing", f"{field_name} is required."
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AcquisitionStagingError(
            "provenance_timestamp_naive", f"{field_name} must include a timezone."
        )
    return parsed


def _tool_name(provider: DiscoveryProvider) -> str:
    return "web_search_exa" if provider == DiscoveryProvider.EXA else "tavily_search"


def _canonical_hash(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
