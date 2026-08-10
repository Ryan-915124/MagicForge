"""Strict data contracts for the local-first localization pipeline."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from hashlib import sha256
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SOURCE_LOCALE = "en-US"
TARGET_LOCALE = "zh-CN"
SOURCE_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MESSAGE_KEY_PATTERN = re.compile(r"^[a-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+$")
PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")


def compute_source_hash(source_text: str) -> str:
    """Return the canonical SHA-256 digest for one English source message."""

    return sha256(source_text.encode("utf-8")).hexdigest()


def extract_placeholders(text: str) -> tuple[str, ...]:
    """Return interpolation names in occurrence order, preserving duplicates."""

    return tuple(PLACEHOLDER_PATTERN.findall(text))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LocalizationClassification(StrEnum):
    UI_TRANSLATION = "ui_translation"
    PRODUCT_NAMING = "product_naming"
    MAGIC_TERMINOLOGY = "magic_terminology"
    ACADEMIC_TERMINOLOGY = "academic_terminology"
    EVIDENCE_GOVERNANCE = "evidence_governance"
    SOURCE_CITATION = "source_citation"
    KNOWLEDGE_CONTENT = "knowledge_content"
    TECHNICAL_IDENTIFIER = "technical_identifier"
    MIXED = "mixed"
    UNCLASSIFIED = "unclassified"


class LocalizationAction(StrEnum):
    PRESERVE_EXACT = "preserve_exact"
    PRESERVE_ENGLISH_ONLY = "preserve_english_only"
    LIMITED_BILINGUAL_SUPPORT = "limited_bilingual_support"
    NORMAL_UI_TRANSLATION = "normal_ui_translation"
    REVIEWED_SUPPLEMENT = "reviewed_supplement"
    SKIP = "skip"


class TermStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    REJECTED = "rejected"


class ProposalStatus(StrEnum):
    MACHINE_PROPOSED = "machine_proposed"
    MACHINE_REVIEWED = "machine_reviewed"
    REJECTED = "rejected"


class QualityReviewDisposition(StrEnum):
    """A second-pass model's non-governance localization recommendation."""

    KEEP = "keep"
    REVISE = "revise"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class QualityReviewNormalization(StrEnum):
    """Safe local rewrites of contradictory model review output."""

    UNCHANGED_REVISION_CONFLICT = "unchanged_revision_conflict"
    MISSING_REVISION_CANDIDATE = "missing_revision_candidate"
    INVALID_REVISION_POLICY = "invalid_revision_policy"


class LocalizationIssueType(StrEnum):
    """Bounded semantic issue vocabulary for localization quality review."""

    SEMANTIC_MISTRANSLATION = "semantic_mistranslation"
    DOMAIN_SENSE = "domain_sense"
    TERMINOLOGY = "terminology"
    CONTEXT_MISMATCH = "context_mismatch"
    UNNATURAL_UI_COPY = "unnatural_ui_copy"
    OVERLY_LITERAL = "overly_literal"
    OVER_TRANSLATION = "over_translation"
    UNDER_TRANSLATION = "under_translation"
    TONE_OR_BRAND_VOICE = "tone_or_brand_voice"
    CONSISTENCY = "consistency"
    AMBIGUITY = "ambiguity"
    GRAMMAR = "grammar"
    PUNCTUATION = "punctuation"
    EPISTEMIC_DRIFT = "epistemic_drift"
    PLACEHOLDER_RISK = "placeholder_risk"
    PROTECTED_TERM_RISK = "protected_term_risk"
    OTHER = "other"


class GenerationOrigin(StrEnum):
    LOCAL_AI = "local_ai"
    DETERMINISTIC = "deterministic"


class ValidationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class SourceUnit(StrictModel):
    """One canonical English UI message and its extraction context."""

    key: str = Field(min_length=3)
    source_text: str = Field(min_length=1)
    source_hash: str
    current_target: str | None = None
    classification: LocalizationClassification = LocalizationClassification.UNCLASSIFIED
    action: LocalizationAction = LocalizationAction.NORMAL_UI_TRANSLATION
    placeholders: tuple[str, ...] = ()
    protected_terms: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    source_locale: Literal["en-US"] = SOURCE_LOCALE
    target_locale: Literal["zh-CN"] = TARGET_LOCALE

    @field_validator("key")
    @classmethod
    def validate_key_shape(cls, value: str) -> str:
        if not MESSAGE_KEY_PATTERN.fullmatch(value):
            raise ValueError("key must be a dot-delimited MagicForge message key")
        return value

    @field_validator("source_hash")
    @classmethod
    def validate_hash_shape(cls, value: str) -> str:
        normalized = value.casefold()
        if not SOURCE_HASH_PATTERN.fullmatch(normalized):
            raise ValueError("source_hash must be a 64-character SHA-256 hex digest")
        return normalized

    @field_validator("placeholders", "protected_terms", "context", mode="before")
    @classmethod
    def normalize_tuple_fields(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(str(item) for item in value)


class TranslationProposal(StrictModel):
    """A local model or deterministic rule's non-authoritative Chinese proposal."""

    key: str = Field(min_length=3)
    source_text: str = Field(min_length=1)
    source_hash: str
    candidate_chinese: str = Field(min_length=1)
    rationale: str = ""
    confidence: float = Field(ge=0.0, le=1.0)
    status: ProposalStatus
    provider: str = Field(min_length=1)
    warnings: tuple[str, ...] = ()
    protected_terms: tuple[str, ...] = ()
    generated_by: GenerationOrigin
    source_locale: Literal["en-US"] = SOURCE_LOCALE
    target_locale: Literal["zh-CN"] = TARGET_LOCALE

    @field_validator("key")
    @classmethod
    def validate_key_shape(cls, value: str) -> str:
        if not MESSAGE_KEY_PATTERN.fullmatch(value):
            raise ValueError("key must be a dot-delimited MagicForge message key")
        return value

    @field_validator("source_hash")
    @classmethod
    def validate_hash_shape(cls, value: str) -> str:
        normalized = value.casefold()
        if not SOURCE_HASH_PATTERN.fullmatch(normalized):
            raise ValueError("source_hash must be a 64-character SHA-256 hex digest")
        return normalized

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        return value.strip().casefold()

    @field_validator("warnings", "protected_terms", mode="before")
    @classmethod
    def normalize_tuple_fields(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(str(item) for item in value)

    @model_validator(mode="after")
    def validate_provenance_combination(self) -> "TranslationProposal":
        allowed = {
            ("glm", GenerationOrigin.LOCAL_AI, ProposalStatus.MACHINE_PROPOSED),
            (
                "deterministic",
                GenerationOrigin.DETERMINISTIC,
                ProposalStatus.MACHINE_REVIEWED,
            ),
        }
        if (self.provider, self.generated_by, self.status) not in allowed:
            raise ValueError(
                "provider, generated_by, and status do not form an implemented provenance combination"
            )
        return self


class QualityReviewDecision(StrictModel):
    """One GLM semantic-review result without approval authority.

    A ``revise`` decision embeds the replacement proposal so its provenance
    and machine-only status remain explicit. ``keep`` and
    ``needs_human_review`` preserve the input proposal verbatim.
    """

    key: str = Field(min_length=3)
    model_disposition: QualityReviewDisposition
    disposition: QualityReviewDisposition
    normalization: QualityReviewNormalization | None = None
    normalization_issue_codes: tuple[str, ...] = ()
    issue_types: tuple[LocalizationIssueType, ...] = ()
    rationale: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
    input_candidate_chinese: str = Field(min_length=1)
    result_candidate_chinese: str = Field(min_length=1)
    input_proposal_sha256: str
    revised_proposal: TranslationProposal | None = None
    reviewer_provider: Literal["glm"] = "glm"

    @field_validator("key")
    @classmethod
    def validate_key_shape(cls, value: str) -> str:
        if not MESSAGE_KEY_PATTERN.fullmatch(value):
            raise ValueError("key must be a dot-delimited MagicForge message key")
        return value

    @field_validator("input_proposal_sha256")
    @classmethod
    def validate_input_hash_shape(cls, value: str) -> str:
        normalized = value.casefold()
        if not SOURCE_HASH_PATTERN.fullmatch(normalized):
            raise ValueError("input_proposal_sha256 must be a 64-character SHA-256 hex digest")
        return normalized

    @field_validator("issue_types", mode="before")
    @classmethod
    def normalize_issue_types(cls, value: object) -> tuple[object, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(value)

    @field_validator("normalization_issue_codes", mode="before")
    @classmethod
    def normalize_issue_codes(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(str(item) for item in value)

    @model_validator(mode="after")
    def validate_disposition_contract(self) -> "QualityReviewDecision":
        if self.normalization is None:
            if self.normalization_issue_codes:
                raise ValueError(
                    "normalization_issue_codes require a recorded normalization"
                )
            if self.model_disposition != self.disposition:
                raise ValueError(
                    "model_disposition may differ only when a normalization is recorded"
                )
        elif self.normalization in {
            QualityReviewNormalization.UNCHANGED_REVISION_CONFLICT,
            QualityReviewNormalization.MISSING_REVISION_CANDIDATE,
            QualityReviewNormalization.INVALID_REVISION_POLICY,
        }:
            if (
                self.model_disposition != QualityReviewDisposition.REVISE
                or self.disposition != QualityReviewDisposition.NEEDS_HUMAN_REVIEW
            ):
                raise ValueError(
                    "revision-conflict normalization must downgrade revise to needs_human_review"
                )
            if (
                self.normalization
                == QualityReviewNormalization.INVALID_REVISION_POLICY
                and not self.normalization_issue_codes
            ):
                raise ValueError(
                    "invalid revision policy normalization requires validator issue codes"
                )
            if (
                self.normalization
                != QualityReviewNormalization.INVALID_REVISION_POLICY
                and self.normalization_issue_codes
            ):
                raise ValueError(
                    "validator issue codes are only valid for invalid revision policy normalization"
                )
        if self.disposition == QualityReviewDisposition.KEEP:
            if self.issue_types:
                raise ValueError("keep decisions cannot declare issue_types")
            if self.revised_proposal is not None:
                raise ValueError("keep decisions cannot contain a revised proposal")
            if self.result_candidate_chinese != self.input_candidate_chinese:
                raise ValueError("keep decisions must preserve the input candidate")
        elif self.disposition == QualityReviewDisposition.REVISE:
            if not self.issue_types:
                raise ValueError("revise decisions require at least one issue_type")
            if self.revised_proposal is None:
                raise ValueError("revise decisions require a revised proposal")
            if self.revised_proposal.key != self.key:
                raise ValueError("revised proposal key must match the review key")
            if self.revised_proposal.candidate_chinese != self.result_candidate_chinese:
                raise ValueError("revised proposal text must match the review result")
            if self.result_candidate_chinese == self.input_candidate_chinese:
                raise ValueError("revise decisions must change the candidate")
            if (
                self.revised_proposal.provider != "glm"
                or self.revised_proposal.generated_by != GenerationOrigin.LOCAL_AI
                or self.revised_proposal.status != ProposalStatus.MACHINE_PROPOSED
            ):
                raise ValueError("revised proposals must remain GLM machine proposals")
        else:
            if not self.issue_types:
                raise ValueError("needs_human_review decisions require an issue_type")
            if self.revised_proposal is not None:
                raise ValueError(
                    "needs_human_review decisions cannot contain a revised proposal"
                )
            if self.result_candidate_chinese != self.input_candidate_chinese:
                raise ValueError(
                    "needs_human_review decisions must preserve the input candidate"
                )
        return self


class GovernedTerm(StrictModel):
    """Normalized glossary and do-not-translate decision for one term."""

    term_id: str
    english: str = Field(min_length=1)
    chinese: str | None = None
    aliases: tuple[str, ...] = ()
    category: str
    status: TermStatus
    display_mode: str
    action: LocalizationAction
    forbidden_display_values: tuple[str, ...] = ()


class PolicyIndex(StrictModel):
    """Immutable in-memory view of MagicForge's machine-readable policies."""

    schema_version: str
    glossary_path: str
    do_not_translate_path: str
    terms: tuple[GovernedTerm, ...]
    exact_terms: tuple[str, ...]
    product_names: tuple[str, ...]
    english_only_terms: tuple[str, ...]
    limited_bilingual_terms: tuple[str, ...]
    protected_knowledge_fields: tuple[str, ...]
    protected_identifier_values: tuple[str, ...]
    forbidden_target_phrases: tuple[str, ...]
    blocked_failures: tuple[str, ...]
    machine_may_set_approved: bool = False
    allowed_machine_providers: tuple[str, ...] = (
        "glm",
        "deterministic",
    )

    @model_validator(mode="after")
    def validate_unique_terms(self) -> "PolicyIndex":
        ids = [term.term_id for term in self.terms]
        english = [term.english for term in self.terms]
        if len(ids) != len(set(ids)):
            raise ValueError("policy contains duplicate term_id values")
        if len(english) != len(set(english)):
            raise ValueError("policy contains duplicate English canonical terms")
        return self

    @property
    def terms_by_id(self) -> dict[str, GovernedTerm]:
        return {term.term_id: term for term in self.terms}

    @property
    def terms_by_english(self) -> dict[str, GovernedTerm]:
        return {term.english: term for term in self.terms}


class ValidationIssue(StrictModel):
    severity: ValidationSeverity
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    key: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(StrictModel):
    valid: bool
    checked_units: int = Field(ge=0)
    checked_proposals: int = Field(ge=0)
    error_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    issues: tuple[ValidationIssue, ...] = ()

    @model_validator(mode="after")
    def validate_summary(self) -> "ValidationReport":
        errors = sum(issue.severity == ValidationSeverity.ERROR for issue in self.issues)
        warnings = sum(issue.severity == ValidationSeverity.WARNING for issue in self.issues)
        if self.error_count != errors or self.warning_count != warnings:
            raise ValueError("validation summary counts do not match issues")
        if self.valid != (errors == 0):
            raise ValueError("valid must reflect whether error issues exist")
        return self

    @classmethod
    def from_issues(
        cls,
        issues: list[ValidationIssue],
        *,
        checked_units: int,
        checked_proposals: int,
    ) -> "ValidationReport":
        counts = Counter(issue.severity for issue in issues)
        return cls(
            valid=counts[ValidationSeverity.ERROR] == 0,
            checked_units=checked_units,
            checked_proposals=checked_proposals,
            error_count=counts[ValidationSeverity.ERROR],
            warning_count=counts[ValidationSeverity.WARNING],
            issues=tuple(issues),
        )


class BatchValidationRequest(StrictModel):
    source_units: tuple[SourceUnit, ...]
    proposals: tuple[TranslationProposal, ...]
