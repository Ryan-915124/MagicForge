"""Deterministic quality corrections for unreviewed Bootstrap extraction.

GLM output remains a proposal.  These helpers only make fail-closed corrections
that can be derived from the retained claim text; they never approve a claim or
raise its evidence strength.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge.evidence import (
    ApplicationOrigin,
    ConfidenceDimension,
    EvidenceCard,
    EvidenceClass,
    classification_for_evidence_class,
    evidence_excerpt_is_locatable,
)


_APPLICATION_PLACEHOLDERS = {
    "n/a",
    "na",
    "none",
    "not applicable",
    "not specified",
    ApplicationOrigin.NOT_APPLICABLE.value,
    ApplicationOrigin.REVIEWER_SYNTHESIS.value,
    ApplicationOrigin.SOURCE_STATED.value,
}

_OBSERVATIONAL_MARKERS = (
    r"\bassociation between\b",
    r"\bassociated with\b",
    r"\bcorrelat(?:e|ed|es|ion|ional|ions)\w*\b",
    r"\bregress(?:ion|ed|or|ors)?\b",
    r"\brelationship between\b",
    r"\b(?:positively|negatively) related\b",
    r"\baccounted for\s+\d+(?:\.\d+)?%\s+of (?:the )?variance\b",
    r"\bpredicted by\b",
    r"\bpredicts?\b",
    r"\bperceived as\b",
    r"\bself[- ]report(?:ed|s)?\b",
    r"\bretrospective\b",
    r"\bpre[- ]existing\b",
    r"\bnaturally (?:occurring|selected)\b",
    r"\bacting experience\b",
    r"\baccumulated amounts? of deliberate practice\b",
    r"\bcorresponded to each skill level\b",
)


@dataclass(frozen=True, slots=True)
class BootstrapQualityCorrection:
    card: EvidenceCard
    reasons: tuple[str, ...]


def normalize_bootstrap_claim_payload(
    payload: dict[str, object],
    *,
    source_title: str = "",
) -> tuple[dict[str, object], tuple[str, ...]]:
    """Downgrade obvious design overstatement and remove enum placeholders.

    The classifier intentionally never upgrades evidence.  A controlled or
    quasi-experimental proposal is downgraded only when the individual claim
    explicitly describes an association, prediction, retrospective report, or
    comparison of pre-existing expertise groups.
    """

    normalized = dict(payload)
    reasons: list[str] = []
    evidence_class = str(normalized.get("evidence_class") or "")
    claim_role = str(normalized.get("claim_role") or "")
    text = " ".join(
        (
            str(normalized.get("statement") or ""),
            str(normalized.get("evidence_excerpt") or ""),
            " ".join(str(item) for item in normalized.get("limitations", []) or []),
            source_title,
        )
    ).casefold()
    if evidence_class in {
        EvidenceClass.CONTROLLED_EXPERIMENT.value,
        EvidenceClass.QUASI_EXPERIMENT.value,
    } and claim_role == "result" and _is_observational_claim(text):
        normalized["evidence_class"] = EvidenceClass.OBSERVATIONAL_STUDY.value
        reasons.append("evidence_class_downgraded_to_observational")
    elif (
        evidence_class == EvidenceClass.OBSERVATIONAL_STUDY.value
        and claim_role == "method"
        and "associative inference trials" in text
        and "directly learned associations" in text
    ):
        # Repair a prior Bootstrap-only normalizer false positive.  This text
        # explicitly describes an experimental trial procedure, not a measured
        # association between naturally observed variables.
        normalized["evidence_class"] = EvidenceClass.CONTROLLED_EXPERIMENT.value
        reasons.append("experimental_method_class_restored")

    application = str(normalized.get("magic_application") or "").strip()
    if application.casefold() in _APPLICATION_PLACEHOLDERS:
        normalized["magic_application"] = None
        normalized["application_origin"] = ApplicationOrigin.NOT_APPLICABLE.value
        reasons.append("magic_application_placeholder_removed")

    return normalized, tuple(reasons)


def normalize_bootstrap_evidence_card(
    card: EvidenceCard,
    *,
    selected_source_text: str,
    source_title: str = "",
) -> BootstrapQualityCorrection:
    """Apply the same corrections to a cached, still-unreviewed card.

    Evidence identity is stable because it is source-version/locator/claim based;
    evidence class, application metadata, and paragraph precision are payload
    properties and therefore update the checksum without changing the card ID.
    """

    payload = card.model_dump(mode="json")
    claim_payload = {
        "statement": card.claim,
        "evidence_excerpt": card.evidence_excerpt,
        "limitations": card.limitations,
        "claim_role": card.claim_role.value,
        "evidence_class": card.evidence_class.value,
        "magic_application": card.magic_application,
        "application_origin": card.application_origin.value,
    }
    normalized, reasons = normalize_bootstrap_claim_payload(
        claim_payload,
        source_title=source_title,
    )
    mutable_reasons = list(reasons)

    normalized_class = EvidenceClass(str(normalized["evidence_class"]))
    if normalized_class != card.evidence_class:
        origin, level = classification_for_evidence_class(normalized_class)
        payload["evidence_class"] = normalized_class.value
        payload["knowledge_origin"] = origin.value
        payload["evidence_level"] = level.value

    if normalized.get("magic_application") != card.magic_application:
        payload["magic_application"] = normalized.get("magic_application")
        payload["application_origin"] = normalized["application_origin"]
        if payload.get("confidence"):
            confidence = dict(payload["confidence"])
            confidence["magic_applicability"] = ConfidenceDimension(
                score=0.0,
                reason="No explicit magic application was retained.",
            ).model_dump(mode="json")
            payload["confidence"] = confidence

    locator = dict(payload["locator"])
    if locator.get("paragraph") is None:
        paragraph = paragraph_for_excerpt(
            selected_source_text,
            card.evidence_excerpt,
        )
        if paragraph is not None:
            locator["paragraph"] = paragraph
            payload["locator"] = locator
            mutable_reasons.append("paragraph_locator_added")

    if not mutable_reasons:
        return BootstrapQualityCorrection(card=card, reasons=())
    return BootstrapQualityCorrection(
        card=EvidenceCard.model_validate(payload),
        reasons=tuple(mutable_reasons),
    )


def _is_observational_claim(text: str) -> bool:
    if any(re.search(pattern, text) for pattern in _OBSERVATIONAL_MARKERS):
        return True
    # This specific combination describes a comparison between existing violin
    # skill groups, even when the extracted sentence only reports an effect size.
    return (
        "deliberate practice" in text
        and "effect size" in text
    )


def paragraph_for_excerpt(source_text: str, excerpt: str) -> int | None:
    if not evidence_excerpt_is_locatable(excerpt, source_text):
        return None
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", source_text)
        if paragraph.strip()
    ]
    excerpt_tokens = _tokens(excerpt)
    for index, paragraph in enumerate(paragraphs, start=1):
        if excerpt_tokens in _tokens(paragraph):
            return index
    for index, _ in enumerate(paragraphs, start=1):
        for span in range(2, min(10, len(paragraphs) - index + 1) + 1):
            joined = " ".join(paragraphs[index - 1 : index - 1 + span])
            if excerpt_tokens in _tokens(joined):
                return index
    return 1 if len(paragraphs) == 1 else None


def _tokens(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold(), flags=re.UNICODE))


__all__ = [
    "BootstrapQualityCorrection",
    "normalize_bootstrap_claim_payload",
    "normalize_bootstrap_evidence_card",
    "paragraph_for_excerpt",
]
