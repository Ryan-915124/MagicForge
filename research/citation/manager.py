"""Citation rules adapted from the citation-management research workflow."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from research.citation.models import (
    CitationRecord,
    CitationStatus,
    PeerReviewStatus,
    VerificationEvidence,
    VerificationMethod,
    VerificationScope,
)
from research.models import ResearchCandidate, SourceCategory


DOI_PATTERN = re.compile(r"^10\.\d{4,9}/[-._;()/:a-z0-9]+$", re.IGNORECASE)


class CitationValidationError(ValueError):
    pass


class CitationManager:
    def from_candidate(self, candidate: ResearchCandidate) -> CitationRecord:
        peer_review_status = (
            PeerReviewStatus.UNKNOWN
            if candidate.source_category == SourceCategory.ACADEMIC
            else PeerReviewStatus.NOT_APPLICABLE
        )
        record = CitationRecord(
            source_candidate_id=candidate.id,
            source_category=candidate.source_category,
            title=candidate.title,
            authors=candidate.authors,
            year=candidate.published_year,
            venue=candidate.venue,
            doi=candidate.doi,
            url=candidate.url,
            peer_review_status=peer_review_status,
            provenance=candidate.provenance,
        )
        return self.validate(record)

    def validate(self, record: CitationRecord) -> CitationRecord:
        missing = []
        errors = []
        if not record.title.strip():
            missing.append("title")
        if not record.url.strip():
            missing.append("url")
        if record.source_category == SourceCategory.ACADEMIC:
            for field_name, value in (
                ("authors", record.authors),
                ("year", record.year),
                ("venue", record.venue),
                ("doi", record.doi),
            ):
                if not value:
                    missing.append(field_name)
            if record.doi and not DOI_PATTERN.fullmatch(record.doi):
                errors.append("invalid DOI format")
        valid_evidence: list[VerificationEvidence] = []
        for evidence in record.verification_evidence:
            problem = _verification_evidence_error(record, evidence)
            if problem:
                errors.append(problem)
            else:
                valid_evidence.append(evidence)

        # Never preserve a caller-supplied verified status.  Verification is
        # derived exclusively from validated, content-addressed evidence.
        if errors:
            status = CitationStatus.FAILED
        elif missing:
            status = CitationStatus.INCOMPLETE
        elif any(
            evidence.scope == VerificationScope.FULL_TEXT
            for evidence in valid_evidence
        ):
            status = CitationStatus.FULL_TEXT_VERIFIED
        elif any(
            evidence.scope == VerificationScope.METADATA
            for evidence in valid_evidence
        ):
            status = CitationStatus.METADATA_VERIFIED
        else:
            status = CitationStatus.UNVERIFIED
        return record.model_copy(
            update={
                "missing_fields": sorted(set(missing)),
                "validation_errors": sorted(set(errors)),
                "status": status,
            }
        )

    def mark_verified(
        self,
        record: CitationRecord,
        *,
        verifier: str,
        verification_source: str,
        full_text: bool = False,
        notes: str = "",
        method: VerificationMethod | None = None,
        resolver_result: Mapping[str, Any] | None = None,
        verified_identifier: str | None = None,
        checked_locator: str | None = None,
        review_actor: str | None = None,
    ) -> CitationRecord:
        checked = self.validate(record)
        if checked.missing_fields or checked.validation_errors:
            raise CitationValidationError(
                "citation cannot be verified while fields are missing or invalid: "
                + ", ".join([*checked.missing_fields, *checked.validation_errors])
            )
        scope = (
            VerificationScope.FULL_TEXT
            if full_text
            else VerificationScope.METADATA
        )
        selected_method = method or (
            VerificationMethod.FULL_TEXT_LOCATOR
            if full_text
            else VerificationMethod.METADATA_RESOLVER
            if checked.doi
            else VerificationMethod.CANONICAL_LOCATOR
        )
        locator = (checked_locator or verification_source).strip()
        actor = (review_actor or verifier).strip()
        identifier = (verified_identifier or checked.doi or checked.url).strip()
        evidence = VerificationEvidence(
            scope=scope,
            method=selected_method,
            resolver_result=dict(
                resolver_result
                or {
                    "matched": True,
                    "verification_source": verification_source.strip(),
                }
            ),
            verified_identifier=identifier,
            checked_locator=locator,
            review_actor=actor,
            notes=notes,
        )
        evidence_items = [*checked.verification_evidence]
        if not any(
            item.evidence_checksum == evidence.evidence_checksum
            for item in evidence_items
        ):
            evidence_items.append(evidence)
        updated = checked.model_copy(
            update={
                "verification_evidence": evidence_items,
            }
        )
        derived = self.validate(updated)
        if derived.status not in {
            CitationStatus.METADATA_VERIFIED,
            CitationStatus.FULL_TEXT_VERIFIED,
        }:
            raise CitationValidationError(
                "citation verification evidence does not match the citation"
            )
        return derived

    def to_bibtex(self, record: CitationRecord) -> str:
        if record.source_category != SourceCategory.ACADEMIC:
            entry_type = "misc"
        else:
            entry_type = "article"
        key = _citation_key(record)
        fields = {
            "author": " and ".join(record.authors),
            "title": record.title,
            "journal": record.venue if entry_type == "article" else "",
            "year": str(record.year or ""),
            "volume": record.volume,
            "number": record.issue,
            "pages": record.pages,
            "doi": record.doi or "",
            "url": record.url,
        }
        rendered = [f"@{entry_type}{{{key},"]
        for name, value in fields.items():
            if value:
                rendered.append(f"  {name} = {{{_bibtex_escape(value)}}},")
        rendered.append("}")
        return "\n".join(rendered)


def _citation_key(record: CitationRecord) -> str:
    first_author = record.authors[0].split()[-1] if record.authors else "Source"
    title_word = next(
        (word for word in re.findall(r"[A-Za-z0-9]+", record.title) if len(word) > 3),
        "Work",
    )
    raw = f"{first_author}{record.year or 'ND'}{title_word}"
    return re.sub(r"[^A-Za-z0-9]", "", raw)


def _bibtex_escape(value: str) -> str:
    return value.replace("\\", "\\textbackslash{}").replace("{", "\\{").replace("}", "\\}")


def _verification_evidence_error(
    record: CitationRecord,
    evidence: VerificationEvidence,
) -> str | None:
    if evidence.evidence_checksum != evidence.calculate_checksum():
        return "verification evidence checksum mismatch"
    if not _identifier_matches(record, evidence.verified_identifier):
        return "verification evidence identifier does not match citation"
    if not _resolver_result_is_match(evidence):
        return "verification resolver did not produce a positive match"
    if evidence.scope == VerificationScope.FULL_TEXT and evidence.method not in {
        VerificationMethod.FULL_TEXT_LOCATOR,
        VerificationMethod.MANUAL_REVIEW,
        VerificationMethod.LEGACY_MIGRATION,
    }:
        return "full-text verification uses an incompatible method"
    if evidence.scope == VerificationScope.METADATA and evidence.method == (
        VerificationMethod.FULL_TEXT_LOCATOR
    ):
        return "metadata verification uses an incompatible method"
    return None


def _resolver_result_is_match(evidence: VerificationEvidence) -> bool:
    result = evidence.resolver_result
    if result.get("matched") is True:
        return True
    if str(result.get("outcome") or "").casefold() == "matched":
        return True
    # Legacy Bootstrap records remain readable, but only the explicit legacy
    # normalization marker is accepted in place of a resolver outcome.
    return bool(result.get("legacy_evidence") is True)


def _identifier_matches(record: CitationRecord, value: str) -> bool:
    candidate = value.strip().casefold()
    if record.doi:
        normalized_doi = _normalize_doi_identifier(candidate)
        if normalized_doi == record.doi.strip().casefold():
            return True
    return _normalize_url(candidate) == _normalize_url(record.url)


def _normalize_doi_identifier(value: str) -> str:
    normalized = value.strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
    match = re.search(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", normalized, re.IGNORECASE)
    return match.group(0).rstrip(".,;)").casefold() if match else normalized


def _normalize_url(value: str) -> str:
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return value.strip().casefold()
    if not parts.scheme or not parts.netloc:
        return value.strip().casefold().rstrip("/")
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path.rstrip("/") or "/",
            parts.query,
            "",
        )
    )
