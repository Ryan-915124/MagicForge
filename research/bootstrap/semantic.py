"""Deterministic entailment checks for bootstrap relationship proposals."""

from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge.evidence import ClaimRole, EvidenceCard
from knowledge.models import EntityType, KnowledgeEntity, RelationType
from research.extraction.models import RelationshipMappingProposal


@dataclass(frozen=True, slots=True)
class RelationshipValidationDecision:
    accepted: bool
    reason: str


_NECESSITY_PATTERNS = (
    r"\brequire(?:s|d)?\b",
    r"\bdepends? on\b",
    r"\bnecessary\b",
    r"\bmust\b",
    r"\bcannot (?:work|occur|be performed) without\b",
    r"\bprerequisite\b",
)
_CORRELATION_PATTERNS = (
    r"\bcorrelat(?:e|ed|es|ion)\b",
    r"\bassociated with\b",
    r"\bactivation\b",
    r"\bactivated\b",
    r"\bco-occur(?:s|red)?\b",
)
_EXPLANATION_PATTERNS = (
    r"\bexplain(?:s|ed)?\b",
    r"\baccounts? for\b",
    r"\bmechanism\b",
    r"\bunderl(?:ies|ying)\b",
    r"\bbecause\b",
    r"\bcontributes? to\b",
    r"\bmediates?\b",
)
_USAGE_PATTERNS = (
    r"\buses?\b",
    r"\bused\b",
    r"\busing\b",
    r"\bemploys?\b",
    r"\bapplies?\b",
    r"\bincorporates?\b",
    r"\bperformed with\b",
)
_PERFORMANCE_PATTERNS = (
    r"\bperformed by\b",
    r"\bperforms?\b",
    r"\bpresents?\b",
    r"\bpresentation by\b",
)
_INSPIRATION_PATTERNS = (
    r"\binspired by\b",
    r"\badapted from\b",
    r"\bbased on\b",
    r"\bderived from\b",
    r"\boriginated with\b",
)
_EXPERIMENT_STIMULUS_PATTERNS = (
    r"\bstimulus\b",
    r"\bexperimental condition\b",
    r"\bcontrol condition\b",
    r"\bvideo condition\b",
    r"\btrial condition\b",
)


def validate_relationship_entailment(
    mapping: RelationshipMappingProposal,
    source: KnowledgeEntity,
    target: KnowledgeEntity,
    cards: list[EvidenceCard],
) -> RelationshipValidationDecision:
    """Fail closed unless the linked claims explicitly entail the edge."""

    relation = mapping.type
    text = _normalized_text(mapping, cards)
    if not cards:
        return RelationshipValidationDecision(False, "relationship has no Evidence Card")
    if any(card.claim_role == ClaimRole.CONTEXT_ONLY for card in cards):
        return RelationshipValidationDecision(False, "context-only text cannot entail an edge")
    if source.id == target.id:
        return RelationshipValidationDecision(False, "self-relations are not useful proposals")

    if relation == RelationType.PERFORMED_BY:
        if source.type != EntityType.EFFECT or target.type != EntityType.PERFORMER:
            return RelationshipValidationDecision(
                False, "performed_by requires Effect -> Performer"
            )
        if not _matches(text, _PERFORMANCE_PATTERNS):
            return RelationshipValidationDecision(
                False, "performed_by lacks an explicit performance statement"
            )
        return RelationshipValidationDecision(True, "explicit Effect -> Performer statement")

    if relation == RelationType.REQUIRES:
        if _matches(text, _CORRELATION_PATTERNS):
            return RelationshipValidationDecision(
                False, "correlation or activation does not entail necessity"
            )
        if not _matches(text, _NECESSITY_PATTERNS):
            return RelationshipValidationDecision(False, "requires lacks necessity language")
        return RelationshipValidationDecision(True, "explicit necessity statement")

    if relation == RelationType.EXPLAINS:
        if not _matches(text, _EXPLANATION_PATTERNS):
            return RelationshipValidationDecision(False, "explains lacks an explanatory claim")
        if not any(
            card.claim_role
            in {ClaimRole.RESULT, ClaimRole.BACKGROUND, ClaimRole.DISCUSSION, ClaimRole.EXPERT_OPINION}
            for card in cards
        ):
            return RelationshipValidationDecision(
                False, "explains is not backed by an explanatory claim role"
            )
        return RelationshipValidationDecision(True, "explicit explanatory statement")

    if relation == RelationType.USES:
        if target.type == EntityType.METHOD and _matches(
            text, _EXPERIMENT_STIMULUS_PATTERNS
        ):
            return RelationshipValidationDecision(
                False, "experiment stimulus or condition is not a magic method"
            )
        if not _matches(text, _USAGE_PATTERNS):
            return RelationshipValidationDecision(False, "uses lacks an actual usage statement")
        return RelationshipValidationDecision(True, "explicit usage statement")

    if relation == RelationType.INSPIRED_BY:
        if not _matches(text, _INSPIRATION_PATTERNS):
            return RelationshipValidationDecision(False, "inspired_by lacks origin language")
        return RelationshipValidationDecision(True, "explicit inspiration statement")

    if relation == RelationType.RELATED_TO:
        stronger_patterns = (
            *_NECESSITY_PATTERNS,
            *_EXPLANATION_PATTERNS,
            *_USAGE_PATTERNS,
            *_PERFORMANCE_PATTERNS,
            *_INSPIRATION_PATTERNS,
        )
        if _matches(text, stronger_patterns):
            return RelationshipValidationDecision(
                False, "related_to is fallback-only and a stronger relation is entailed"
            )
        source_name = source.name.casefold()
        target_name = target.name.casefold()
        if source_name not in text or target_name not in text:
            return RelationshipValidationDecision(
                False, "related_to requires explicit co-reference to both entities"
            )
        return RelationshipValidationDecision(True, "explicit co-reference with no stronger edge")

    return RelationshipValidationDecision(False, "unsupported relationship type")


def _normalized_text(
    mapping: RelationshipMappingProposal,
    cards: list[EvidenceCard],
) -> str:
    values = [
        mapping.assertion,
        mapping.evidence_excerpt,
        *(card.claim for card in cards),
        *(card.evidence_excerpt for card in cards),
    ]
    return " ".join(" ".join(value.casefold().split()) for value in values)


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


__all__ = ["RelationshipValidationDecision", "validate_relationship_entailment"]
