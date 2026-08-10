"""Evidence Card schema separating extraction confidence from evidence strength."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from statistics import mean
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from knowledge.governance import (
    ClaimEligibility,
    ContradictionCheckStatus,
    ExtractionPermission,
    ReviewStatus,
    SecretExposureLevel,
    SensitiveInformationLevel,
    StoragePermission,
)
from knowledge.ontology import normalize_ontology_paths


EVIDENCE_SCHEMA_VERSION = "evidence-0.2"


class KnowledgeOrigin(StrEnum):
    SCIENTIFIC_EVIDENCE = "scientific_evidence"
    EXPERT_PRACTICE = "expert_practice"
    PERSONAL_INTERPRETATION = "personal_interpretation"


class EvidenceLevel(StrEnum):
    EMPIRICAL = "empirical"
    REVIEW = "review"
    PRACTITIONER = "practitioner"
    ANECDOTAL = "anecdotal"


class EvidenceClass(StrEnum):
    CONTROLLED_EXPERIMENT = "controlled_experiment"
    QUASI_EXPERIMENT = "quasi_experiment"
    OBSERVATIONAL_STUDY = "observational_study"
    SYSTEMATIC_REVIEW = "systematic_review"
    META_ANALYSIS = "meta_analysis"
    NARRATIVE_REVIEW = "narrative_review"
    EXPERT_INSTRUCTION = "expert_instruction"
    EXPERT_CASE_ANALYSIS = "expert_case_analysis"
    PRACTITIONER_REPORT = "practitioner_report"
    HISTORICAL_PRIMARY_RECORD = "historical_primary_record"
    HISTORICAL_SECONDARY_ANALYSIS = "historical_secondary_analysis"
    ANALYST_INTERPRETATION = "analyst_interpretation"
    ANECDOTAL_OBSERVATION = "anecdotal_observation"


class ClaimRole(StrEnum):
    RESULT = "result"
    METHOD = "method"
    BACKGROUND = "background"
    HYPOTHESIS = "hypothesis"
    DISCUSSION = "discussion"
    EXPERT_OPINION = "expert_opinion"
    CONTEXT_ONLY = "context_only"


_EVIDENCE_CLASSIFICATION: dict[
    EvidenceClass, tuple[KnowledgeOrigin, EvidenceLevel]
] = {
    EvidenceClass.CONTROLLED_EXPERIMENT: (
        KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        EvidenceLevel.EMPIRICAL,
    ),
    EvidenceClass.QUASI_EXPERIMENT: (
        KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        EvidenceLevel.EMPIRICAL,
    ),
    EvidenceClass.OBSERVATIONAL_STUDY: (
        KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        EvidenceLevel.EMPIRICAL,
    ),
    EvidenceClass.SYSTEMATIC_REVIEW: (
        KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        EvidenceLevel.REVIEW,
    ),
    EvidenceClass.META_ANALYSIS: (
        KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        EvidenceLevel.REVIEW,
    ),
    EvidenceClass.NARRATIVE_REVIEW: (
        KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        EvidenceLevel.REVIEW,
    ),
    EvidenceClass.EXPERT_INSTRUCTION: (
        KnowledgeOrigin.EXPERT_PRACTICE,
        EvidenceLevel.PRACTITIONER,
    ),
    EvidenceClass.EXPERT_CASE_ANALYSIS: (
        KnowledgeOrigin.EXPERT_PRACTICE,
        EvidenceLevel.PRACTITIONER,
    ),
    EvidenceClass.PRACTITIONER_REPORT: (
        KnowledgeOrigin.EXPERT_PRACTICE,
        EvidenceLevel.ANECDOTAL,
    ),
    EvidenceClass.HISTORICAL_PRIMARY_RECORD: (
        KnowledgeOrigin.EXPERT_PRACTICE,
        EvidenceLevel.ANECDOTAL,
    ),
    EvidenceClass.HISTORICAL_SECONDARY_ANALYSIS: (
        KnowledgeOrigin.EXPERT_PRACTICE,
        EvidenceLevel.PRACTITIONER,
    ),
    EvidenceClass.ANALYST_INTERPRETATION: (
        KnowledgeOrigin.PERSONAL_INTERPRETATION,
        EvidenceLevel.ANECDOTAL,
    ),
    EvidenceClass.ANECDOTAL_OBSERVATION: (
        KnowledgeOrigin.PERSONAL_INTERPRETATION,
        EvidenceLevel.ANECDOTAL,
    ),
}


_EMPIRICAL_CLASSES = {
    EvidenceClass.CONTROLLED_EXPERIMENT,
    EvidenceClass.QUASI_EXPERIMENT,
    EvidenceClass.OBSERVATIONAL_STUDY,
}

_REVIEW_CLASSES = {
    EvidenceClass.SYSTEMATIC_REVIEW,
    EvidenceClass.META_ANALYSIS,
    EvidenceClass.NARRATIVE_REVIEW,
}

_EXPERT_CLASSES = {
    EvidenceClass.EXPERT_INSTRUCTION,
    EvidenceClass.EXPERT_CASE_ANALYSIS,
    EvidenceClass.PRACTITIONER_REPORT,
    EvidenceClass.HISTORICAL_PRIMARY_RECORD,
    EvidenceClass.HISTORICAL_SECONDARY_ANALYSIS,
}

_INTERPRETATION_CLASSES = {
    EvidenceClass.ANALYST_INTERPRETATION,
    EvidenceClass.ANECDOTAL_OBSERVATION,
}

_ALLOWED_CLASSES_BY_CLAIM_ROLE: dict[ClaimRole, set[EvidenceClass]] = {
    ClaimRole.RESULT: {
        *_EMPIRICAL_CLASSES,
        EvidenceClass.SYSTEMATIC_REVIEW,
        EvidenceClass.META_ANALYSIS,
        EvidenceClass.EXPERT_CASE_ANALYSIS,
        EvidenceClass.PRACTITIONER_REPORT,
        EvidenceClass.ANECDOTAL_OBSERVATION,
    },
    ClaimRole.METHOD: {
        *_EMPIRICAL_CLASSES,
        *_REVIEW_CLASSES,
        EvidenceClass.EXPERT_INSTRUCTION,
        EvidenceClass.PRACTITIONER_REPORT,
        EvidenceClass.HISTORICAL_PRIMARY_RECORD,
        EvidenceClass.HISTORICAL_SECONDARY_ANALYSIS,
    },
    ClaimRole.BACKGROUND: {
        *_REVIEW_CLASSES,
        *_EXPERT_CLASSES,
        *_INTERPRETATION_CLASSES,
    },
    ClaimRole.HYPOTHESIS: {
        EvidenceClass.NARRATIVE_REVIEW,
        EvidenceClass.EXPERT_INSTRUCTION,
        EvidenceClass.EXPERT_CASE_ANALYSIS,
        EvidenceClass.PRACTITIONER_REPORT,
        *_INTERPRETATION_CLASSES,
    },
    ClaimRole.DISCUSSION: {
        *_REVIEW_CLASSES,
        EvidenceClass.EXPERT_CASE_ANALYSIS,
        EvidenceClass.PRACTITIONER_REPORT,
        *_INTERPRETATION_CLASSES,
    },
    ClaimRole.EXPERT_OPINION: {*_EXPERT_CLASSES, *_INTERPRETATION_CLASSES},
    ClaimRole.CONTEXT_ONLY: set(EvidenceClass),
}

_UNCERTAINTY_MARKERS = (
    "may",
    "might",
    "could",
    "can",
    "suggest",
    "appears",
    "apparently",
    "seems",
    "likely",
    "possibly",
    "potentially",
    "unclear",
    "uncertain",
    "consistent with",
    "cannot rule out",
    "evidence to suggest",
)

_SCOPE_MARKERS = (
    "all",
    "always",
    "never",
    "most people",
    "everyone",
    "no one",
    "causes",
    "proves",
    "requires",
)


def classification_for_evidence_class(
    evidence_class: EvidenceClass,
) -> tuple[KnowledgeOrigin, EvidenceLevel]:
    """Return the only valid epistemic channel for an evidence class."""

    return _EVIDENCE_CLASSIFICATION[evidence_class]


def validate_claim_role_class(
    claim_role: ClaimRole,
    evidence_class: EvidenceClass,
) -> None:
    if evidence_class not in _ALLOWED_CLASSES_BY_CLAIM_ROLE[claim_role]:
        raise ValueError(
            f"{claim_role.value} claims cannot use {evidence_class.value}"
        )


def validate_claim_fidelity(
    statement: str,
    excerpt: str,
    *,
    claim_role: ClaimRole,
) -> None:
    """Reject deterministic scope or uncertainty inflation."""

    normalized_statement = " ".join(statement.casefold().split())
    normalized_excerpt = " ".join(excerpt.casefold().split())
    excerpt_uncertainty = {
        marker for marker in _UNCERTAINTY_MARKERS if marker in normalized_excerpt
    }
    if excerpt_uncertainty and not any(
        marker in normalized_statement for marker in _UNCERTAINTY_MARKERS
    ):
        raise ValueError("claim removes uncertainty language present in the excerpt")
    added_scope = {
        marker
        for marker in _SCOPE_MARKERS
        if _contains_marker(normalized_statement, marker)
        and not _contains_marker(normalized_excerpt, marker)
    }
    if added_scope:
        raise ValueError(
            "claim adds unsupported scope or causal force: "
            + ", ".join(sorted(added_scope))
        )


def evidence_excerpt_is_locatable(excerpt: str, source_text: str) -> bool:
    """Match an excerpt to source text while ignoring punctuation typography."""

    def tokens(value: str) -> str:
        return " ".join(re.findall(r"\w+", value.casefold(), flags=re.UNICODE))

    normalized_excerpt = tokens(excerpt)
    return bool(normalized_excerpt and normalized_excerpt in tokens(source_text))


def _contains_marker(text: str, marker: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", text))


class ClaimPolarity(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    QUALIFIES = "qualifies"


class MechanismStatus(StrEnum):
    LINKED = "linked"
    UNRESOLVED = "unresolved"
    NOT_APPLICABLE = "not_applicable"


class ApplicationOrigin(StrEnum):
    SOURCE_STATED = "source_stated"
    REVIEWER_SYNTHESIS = "reviewer_synthesis"
    NOT_APPLICABLE = "not_applicable"


class MagicDomain(StrEnum):
    CARD = "card"
    CLOSE_UP = "close-up"
    STAGE = "stage"
    MENTALISM = "mentalism"
    THEORY = "theory"


class SourceType(StrEnum):
    JOURNAL_ARTICLE = "journal_article"
    CONFERENCE_PAPER = "conference_paper"
    PREPRINT = "preprint"
    ACADEMIC_BOOK = "academic_book"
    BOOK_CHAPTER = "book_chapter"
    PRACTITIONER_BOOK = "practitioner_book"
    WEB_ARTICLE = "web_article"
    INTERVIEW = "interview"
    TRANSCRIPT = "transcript"
    ARCHIVAL_MATERIAL = "archival_material"
    INTERNAL_ANALYSIS = "internal_analysis"


class ContradictionStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    NONE_FOUND = "none_found"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class ConfidenceLabel(StrEnum):
    INSUFFICIENT = "insufficient"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class ConfidenceDimension(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float
    reason: str = Field(min_length=1)

    @field_validator("score")
    @classmethod
    def validate_scale(cls, value: float) -> float:
        if value not in {0.0, 0.5, 1.0}:
            raise ValueError("confidence dimensions use only 0.0, 0.5, or 1.0")
        return value


class ConfidenceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provenance_quality: ConfidenceDimension
    method_rigor: ConfidenceDimension
    claim_directness: ConfidenceDimension
    consistency: ConfidenceDimension
    magic_applicability: ConfidenceDimension
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    label: ConfidenceLabel = ConfidenceLabel.INSUFFICIENT
    assessed_by: str = Field(min_length=1)
    assessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def calculate_score(self) -> "ConfidenceAssessment":
        score = mean(
            (
                self.provenance_quality.score,
                self.method_rigor.score,
                self.claim_directness.score,
                self.consistency.score,
                self.magic_applicability.score,
            )
        )
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "label", _label_for_score(score))
        return self


class EvidenceSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    citation_id: str
    source_id: str
    source_candidate_id: str
    source_version_id: str
    document_id: str
    source_type: SourceType
    research_paper_id: str | None = None
    citation_status: str
    peer_review_status: str
    source_year: int | None = Field(default=None, ge=1800, le=2200)

    @field_validator(
        "citation_id",
        "source_id",
        "source_candidate_id",
        "source_version_id",
        "document_id",
        "research_paper_id",
    )
    @classmethod
    def validate_uuid_fields(cls, value: str | None) -> str | None:
        if value is not None:
            UUID(value)
        return value


class EvidenceLocator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    media_type: str = Field(min_length=1)
    source_locator: str = Field(min_length=1)
    page_number: int | None = Field(default=None, ge=1)
    printed_page: str | None = None
    section: str | None = None
    paragraph: int | None = Field(default=None, ge=1)
    figure_or_table: str | None = None
    timestamp_start: float | None = Field(default=None, ge=0.0)
    timestamp_end: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def validate_media_locator(self) -> "EvidenceLocator":
        if self.media_type == "pdf" and self.page_number is None:
            raise ValueError("PDF evidence requires page_number")
        if (
            self.timestamp_start is not None
            and self.timestamp_end is not None
            and self.timestamp_end < self.timestamp_start
        ):
            raise ValueError("timestamp_end must not precede timestamp_start")
        return self


class EvidenceReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review_item_id: str | None = None
    claim_eligibility: ClaimEligibility = ClaimEligibility.NOT_ASSESSED
    extraction_permission: ExtractionPermission = ExtractionPermission.NONE
    storage_permission: StoragePermission = StoragePermission.NONE
    approved: bool = False
    review_status: ReviewStatus = ReviewStatus.PENDING
    reviewer: str | None = None
    review_date: datetime | None = None
    approval_reason: str | None = None
    contradicting_evidence_checked: ContradictionCheckStatus = (
        ContradictionCheckStatus.NOT_CHECKED
    )
    sensitive_information_level: SensitiveInformationLevel = (
        SensitiveInformationLevel.CONTROLLED
    )

    @field_validator("review_item_id")
    @classmethod
    def validate_review_id(cls, value: str | None) -> str | None:
        if value is not None:
            UUID(value)
        return value

    @model_validator(mode="after")
    def enforce_review_state(self) -> "EvidenceReview":
        if self.approved != (self.review_status in {ReviewStatus.APPROVED, ReviewStatus.INGESTED}):
            raise ValueError("approved must match approved/ingested review status")
        if self.approved:
            if not self.reviewer or not self.review_date or not self.approval_reason:
                raise ValueError("approved evidence requires reviewer, date, and reason")
            if not self.claim_eligibility.allows_projection:
                raise ValueError("approved evidence must be claim-eligible")
            if not self.contradicting_evidence_checked.completed:
                raise ValueError("approved evidence requires a completed contradiction check")
        return self


class EvidenceCard(BaseModel):
    """One source/version/locator supporting one atomic, reviewable claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    schema_version: str = EVIDENCE_SCHEMA_VERSION
    version: int = Field(default=1, ge=1)
    canonical_claim_id: str = ""
    claim: str = Field(min_length=1)
    claim_role: ClaimRole
    claim_polarity: ClaimPolarity = ClaimPolarity.SUPPORTS
    mechanism_ids: list[str] = Field(default_factory=list)
    mechanism_status: MechanismStatus = MechanismStatus.UNRESOLVED
    principle_ids: list[str] = Field(default_factory=list)
    applicable_domain: list[MagicDomain] = Field(min_length=1)
    ontology_paths: list[str] = Field(min_length=1)
    topic_tags: list[str] = Field(default_factory=list)
    magic_application: str | None = None
    application_origin: ApplicationOrigin = ApplicationOrigin.NOT_APPLICABLE
    knowledge_origin: KnowledgeOrigin
    evidence_class: EvidenceClass
    evidence_level: EvidenceLevel
    source: EvidenceSource
    locator: EvidenceLocator
    evidence_excerpt: str = Field(min_length=1, max_length=1_000)
    excerpt_hash: str = ""
    limitations: list[str] = Field(min_length=1)
    population_context: str | None = None
    performance_context: str | None = None
    confidence: ConfidenceAssessment | None = None
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    contradiction_status: ContradictionStatus = ContradictionStatus.NOT_CHECKED
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    supersedes: list[str] = Field(default_factory=list)
    review: EvidenceReview = Field(default_factory=EvidenceReview)
    secret_exposure_level: SecretExposureLevel = SecretExposureLevel.GENERAL_PRINCIPLE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str = Field(min_length=1)

    @field_validator("claim")
    @classmethod
    def normalize_claim(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("ontology_paths")
    @classmethod
    def normalize_paths(cls, values: list[str]) -> list[str]:
        return normalize_ontology_paths(values)

    @field_validator(
        "mechanism_ids",
        "principle_ids",
        "contradicting_evidence_ids",
        "supersedes",
    )
    @classmethod
    def validate_uuid_lists(cls, values: list[str]) -> list[str]:
        for value in values:
            UUID(value)
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def assign_identity_and_validate(self) -> "EvidenceCard":
        if self.claim_role == ClaimRole.CONTEXT_ONLY:
            raise ValueError("context_only claims cannot create Evidence Cards")
        validate_claim_role_class(self.claim_role, self.evidence_class)
        validate_claim_fidelity(
            self.claim,
            self.evidence_excerpt,
            claim_role=self.claim_role,
        )
        expected_origin, expected_level = _EVIDENCE_CLASSIFICATION[self.evidence_class]
        if self.knowledge_origin != expected_origin or self.evidence_level != expected_level:
            raise ValueError(
                f"{self.evidence_class.value} requires "
                f"{expected_origin.value}/{expected_level.value}"
            )
        if self.mechanism_status == MechanismStatus.LINKED and not self.mechanism_ids:
            raise ValueError("linked mechanism status requires mechanism_ids")
        if self.mechanism_ids and self.mechanism_status != MechanismStatus.LINKED:
            raise ValueError("mechanism_ids require linked mechanism status")
        if self.magic_application and self.application_origin == ApplicationOrigin.NOT_APPLICABLE:
            raise ValueError("magic_application requires an explicit application_origin")
        if not self.magic_application and self.application_origin != ApplicationOrigin.NOT_APPLICABLE:
            raise ValueError("application_origin requires magic_application")

        normalized_excerpt = " ".join(self.evidence_excerpt.split())
        excerpt_hash = hashlib.sha256(normalized_excerpt.encode("utf-8")).hexdigest()
        if self.excerpt_hash and self.excerpt_hash != excerpt_hash:
            raise ValueError("excerpt_hash does not match evidence_excerpt")
        object.__setattr__(self, "excerpt_hash", excerpt_hash)

        claim_identity = str(
            uuid5(NAMESPACE_URL, f"magicforge:claim:{self.claim.casefold()}")
        )
        if self.canonical_claim_id:
            UUID(self.canonical_claim_id)
        else:
            object.__setattr__(self, "canonical_claim_id", claim_identity)

        identity = ":".join(
            (
                self.source.source_version_id,
                self.locator.source_locator.casefold(),
                self.claim.casefold(),
                str(self.version),
                self.schema_version,
            )
        )
        if self.id:
            UUID(self.id)
        else:
            object.__setattr__(
                self,
                "id",
                str(uuid5(NAMESPACE_URL, f"magicforge:evidence:{identity}")),
            )

        if self.review.approved:
            if self.confidence is None:
                raise ValueError("approved evidence requires a confidence assessment")
            if self.contradiction_status == ContradictionStatus.NOT_CHECKED:
                raise ValueError("approved evidence requires contradiction status")
            if self.confidence.label == ConfidenceLabel.INSUFFICIENT:
                raise ValueError("insufficient evidence cannot be approved")
            capped_label = _cap_confidence_label(self)
            if capped_label != self.confidence.label:
                object.__setattr__(
                    self,
                    "confidence",
                    self.confidence.model_copy(update={"label": capped_label}),
                )
        return self

    @property
    def projection_eligible(self) -> bool:
        return bool(
            self.review.approved
            and self.review.claim_eligibility.allows_projection
            and self.review.storage_permission.allows_projection
            and self.review.contradicting_evidence_checked.completed
            and self.contradiction_status != ContradictionStatus.NOT_CHECKED
            and self.confidence is not None
            and self.confidence.label != ConfidenceLabel.INSUFFICIENT
        )


def _label_for_score(score: float) -> ConfidenceLabel:
    if score < 0.40:
        return ConfidenceLabel.INSUFFICIENT
    if score < 0.60:
        return ConfidenceLabel.LOW
    if score < 0.80:
        return ConfidenceLabel.MODERATE
    return ConfidenceLabel.HIGH


def _cap_confidence_label(card: EvidenceCard) -> ConfidenceLabel:
    assert card.confidence is not None
    label = card.confidence.label
    if card.knowledge_origin == KnowledgeOrigin.PERSONAL_INTERPRETATION:
        return min(label, ConfidenceLabel.LOW, key=_label_rank)
    if card.knowledge_origin == KnowledgeOrigin.EXPERT_PRACTICE:
        label = min(label, ConfidenceLabel.MODERATE, key=_label_rank)
    if card.evidence_class in {
        EvidenceClass.CONTROLLED_EXPERIMENT,
        EvidenceClass.QUASI_EXPERIMENT,
        EvidenceClass.OBSERVATIONAL_STUDY,
    }:
        label = min(label, ConfidenceLabel.MODERATE, key=_label_rank)
    if card.contradiction_status == ContradictionStatus.UNRESOLVED:
        label = min(label, ConfidenceLabel.MODERATE, key=_label_rank)
    return label


def _label_rank(label: ConfidenceLabel) -> int:
    return {
        ConfidenceLabel.INSUFFICIENT: 0,
        ConfidenceLabel.LOW: 1,
        ConfidenceLabel.MODERATE: 2,
        ConfidenceLabel.HIGH: 3,
    }[label]


__all__ = [
    "ApplicationOrigin",
    "ClaimRole",
    "ClaimPolarity",
    "ConfidenceAssessment",
    "ConfidenceDimension",
    "ConfidenceLabel",
    "ContradictionStatus",
    "EvidenceCard",
    "EvidenceClass",
    "EvidenceLevel",
    "EvidenceLocator",
    "EvidenceReview",
    "EvidenceSource",
    "KnowledgeOrigin",
    "MagicDomain",
    "MechanismStatus",
    "SourceType",
    "classification_for_evidence_class",
    "validate_claim_fidelity",
    "validate_claim_role_class",
    "evidence_excerpt_is_locatable",
]
