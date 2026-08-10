from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from research.citation.manager import CitationManager
from research.citation.models import (
    CitationRecord,
    CitationStatus,
    VerificationEvidence,
    VerificationMethod,
    VerificationScope,
)
from research.models import SourceCategory


def _complete_citation(**updates) -> CitationRecord:
    values = {
        "source_candidate_id": str(uuid4()),
        "source_category": SourceCategory.ACADEMIC,
        "title": "Attention and Magic",
        "authors": ["A. Researcher"],
        "year": 2026,
        "venue": "Journal of Magic Cognition",
        "doi": "10.1234/magic.2026.1",
        "url": "https://example.test/articles/magic-2026-1",
    }
    values.update(updates)
    return CitationRecord(**values)


@pytest.mark.parametrize(
    "forged_status",
    [CitationStatus.METADATA_VERIFIED, CitationStatus.FULL_TEXT_VERIFIED],
)
def test_validate_does_not_trust_caller_supplied_verified_status(
    forged_status: CitationStatus,
) -> None:
    record = _complete_citation(status=forged_status, verification_evidence=[])

    validated = CitationManager().validate(record)

    assert validated.status == CitationStatus.UNVERIFIED
    assert validated.verification_evidence == []


def test_incomplete_metadata_cannot_be_escalated_by_status() -> None:
    record = _complete_citation(
        authors=[],
        doi=None,
        status=CitationStatus.FULL_TEXT_VERIFIED,
    )

    validated = CitationManager().validate(record)

    assert validated.status == CitationStatus.INCOMPLETE
    assert {"authors", "doi"}.issubset(validated.missing_fields)


def test_mark_verified_creates_complete_content_addressed_evidence() -> None:
    manager = CitationManager()

    verified = manager.mark_verified(
        _complete_citation(),
        verifier="Ryan Chen",
        verification_source="https://api.crossref.org/works/10.1234/magic.2026.1",
        resolver_result={"matched": True, "provider": "crossref"},
    )

    assert verified.status == CitationStatus.METADATA_VERIFIED
    evidence = verified.verification_evidence[0]
    assert evidence.scope == VerificationScope.METADATA
    assert evidence.method == VerificationMethod.METADATA_RESOLVER
    assert evidence.verified_identifier == "10.1234/magic.2026.1"
    assert evidence.checked_locator.startswith("https://api.crossref.org/")
    assert evidence.review_actor == "Ryan Chen"
    assert len(evidence.evidence_checksum) == 64
    assert evidence.evidence_checksum == evidence.calculate_checksum()

    with pytest.raises(ValidationError):
        evidence.review_actor = "Different actor"
    with pytest.raises(TypeError, match="immutable"):
        evidence.resolver_result["matched"] = False


def test_full_text_status_is_derived_from_full_text_evidence() -> None:
    verified = CitationManager().mark_verified(
        _complete_citation(status=CitationStatus.UNVERIFIED),
        verifier="Ryan Chen",
        verification_source="https://example.test/articles/magic-2026-1",
        full_text=True,
    )

    assert verified.status == CitationStatus.FULL_TEXT_VERIFIED
    assert verified.verification_evidence[-1].scope == VerificationScope.FULL_TEXT


def test_checksum_tampering_is_rejected() -> None:
    evidence = VerificationEvidence(
        scope=VerificationScope.METADATA,
        method=VerificationMethod.METADATA_RESOLVER,
        resolver_result={"matched": True},
        verified_identifier="10.1234/magic.2026.1",
        checked_locator="https://api.crossref.org/works/10.1234/magic.2026.1",
        review_actor="Ryan Chen",
    )
    payload = evidence.model_dump(mode="python")
    payload["review_actor"] = "Tampered Actor"

    with pytest.raises(ValidationError, match="checksum does not match"):
        VerificationEvidence.model_validate(payload)


def test_negative_resolver_outcome_cannot_verify_a_citation() -> None:
    evidence = VerificationEvidence(
        scope=VerificationScope.METADATA,
        method=VerificationMethod.METADATA_RESOLVER,
        resolver_result={"matched": False, "provider": "crossref"},
        verified_identifier="10.1234/magic.2026.1",
        checked_locator="https://api.crossref.org/works/10.1234/magic.2026.1",
        review_actor="Ryan Chen",
    )
    record = _complete_citation(verification_evidence=[evidence])

    validated = CitationManager().validate(record)

    assert validated.status == CitationStatus.FAILED
    assert "verification resolver did not produce a positive match" in (
        validated.validation_errors
    )


def test_metadata_evidence_cannot_be_upgraded_by_record_status() -> None:
    metadata_verified = CitationManager().mark_verified(
        _complete_citation(),
        verifier="Ryan Chen",
        verification_source="https://api.crossref.org/works/10.1234/magic.2026.1",
    )
    forged = metadata_verified.model_copy(
        update={"status": CitationStatus.FULL_TEXT_VERIFIED}
    )

    assert CitationManager().validate(forged).status == CitationStatus.METADATA_VERIFIED


def test_legacy_bootstrap_evidence_shape_remains_readable() -> None:
    evidence = VerificationEvidence(
        verifier="crossref-exact-metadata-match",
        verification_source="https://api.crossref.org/works/10.1234/magic.2026.1",
        notes="Legacy Bootstrap metadata evidence.",
    )
    record = _complete_citation(
        status=CitationStatus.FULL_TEXT_VERIFIED,
        verification_evidence=[evidence],
    )

    validated = CitationManager().validate(record)

    assert validated.status == CitationStatus.METADATA_VERIFIED
    assert evidence.verifier == evidence.review_actor
    assert evidence.verification_source == evidence.checked_locator
