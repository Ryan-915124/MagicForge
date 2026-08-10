"""Production Source/Claim workflow contracts for Checkpoint 3.

These models describe commands and safe service results.  They deliberately do
not expose writable ``verified`` or ``approved`` booleans: both states are
derived by the application service from immutable evidence and review rows.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from knowledge.evidence import (
    ApplicationOrigin,
    ClaimPolarity,
    ClaimRole,
    ConfidenceAssessment,
    ContradictionStatus,
    EvidenceClass,
    EvidenceLocator,
    KnowledgeOrigin,
    MagicDomain,
    MechanismStatus,
    SourceType,
    validate_claim_fidelity,
    validate_claim_role_class,
)
from knowledge.governance import (
    ClaimEligibility,
    ContradictionCheckStatus,
    ExtractionPermission,
    SecretExposureLevel,
    SensitiveInformationLevel,
    StoragePermission,
)
from knowledge.ontology import normalize_ontology_paths
from research.citation.models import PeerReviewStatus
from research.models import ContentAccess, SearchProvenance, SourceCategory


SOURCE_WORKFLOW_SCHEMA_VERSION = "source-workflow-0.1"
SOURCE_PERMISSION_REQUEST_SCHEMA_VERSION = "source-permission-request-0.1"
CLAIM_CANDIDATE_SCHEMA_VERSION = "claim-candidate-0.1"
REVIEW_POLICY_VERSION = "review-policy-0.1"


class WorkflowStatus(StrEnum):
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class ReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class CitationVerificationScope(StrEnum):
    METADATA = "metadata"
    FULL_TEXT = "full_text"


class CitationVerificationMethod(StrEnum):
    DOI_RESOLVER = "doi_resolver"
    CANONICAL_LOCATOR = "canonical_locator"
    MANUAL_METADATA = "manual_metadata"
    CONTENT_CHECKSUM = "content_checksum"


class ResolverResult(StrEnum):
    MATCHED = "matched"
    MISMATCH = "mismatch"
    NOT_FOUND = "not_found"
    ERROR = "error"


class ExtractionProducer(StrEnum):
    HUMAN = "human"
    GLM = "glm"
    PIPELINE = "pipeline"


class CitationMetadataInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=2_000)
    authors: list[str] = Field(default_factory=list, max_length=200)
    year: int | None = Field(default=None, ge=1800, le=2200)
    venue: str = Field(default="", max_length=1_000)
    volume: str = Field(default="", max_length=100)
    issue: str = Field(default="", max_length=100)
    pages: str = Field(default="", max_length=100)
    doi: str | None = Field(default=None, max_length=500)
    url: str = Field(min_length=1, max_length=4_000)
    peer_review_status: PeerReviewStatus = PeerReviewStatus.UNKNOWN
    provenance: list[SearchProvenance] = Field(min_length=1, max_length=100)

    @field_validator("doi", mode="before")
    @classmethod
    def normalize_doi(cls, value: object) -> str | None:
        if value is None or not str(value).strip():
            return None
        normalized = str(value).strip().casefold()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
        return normalized

    @field_validator("authors")
    @classmethod
    def normalize_authors(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class CitationVerificationInput(BaseModel):
    """An attested check, never a client-selected verification state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: CitationVerificationScope
    method: CitationVerificationMethod
    resolver_result: ResolverResult
    verified_identifier: str = Field(min_length=1, max_length=4_000)
    checked_locator: str = Field(min_length=1, max_length=4_000)
    resolver_name: str = Field(min_length=1, max_length=200)
    notes: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def validate_method_scope(self) -> "CitationVerificationInput":
        if (
            self.scope == CitationVerificationScope.FULL_TEXT
            and self.method != CitationVerificationMethod.CONTENT_CHECKSUM
        ):
            raise ValueError("full_text verification requires content_checksum")
        if (
            self.method == CitationVerificationMethod.CONTENT_CHECKSUM
            and self.scope != CitationVerificationScope.FULL_TEXT
        ):
            raise ValueError("content_checksum is only valid for full_text verification")
        return self


class SourceAccessMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    access_method: str = Field(min_length=1, max_length=300)
    license_name: str | None = Field(default=None, max_length=300)
    rights_uri: str | None = Field(default=None, max_length=4_000)
    permission_notes: str = Field(default="", max_length=2_000)
    redistribution_allowed: bool = False


class SourceVersionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["source-workflow-0.1"] = SOURCE_WORKFLOW_SCHEMA_VERSION
    citation: CitationMetadataInput
    source_category: SourceCategory
    source_type: SourceType
    knowledge_origin: KnowledgeOrigin
    content: str = Field(min_length=1, max_length=10_000_000)
    content_access: ContentAccess
    access: SourceAccessMetadata
    requested_extraction_permission: ExtractionPermission = ExtractionPermission.NONE
    requested_storage_permission: StoragePermission = StoragePermission.NONE
    requested_scope_locators: list[str] = Field(default_factory=list, max_length=500)
    sensitive_information_level: SensitiveInformationLevel = (
        SensitiveInformationLevel.CONTROLLED
    )
    citation_verification: list[CitationVerificationInput] = Field(
        default_factory=list, max_length=20
    )

    @field_validator("requested_scope_locators")
    @classmethod
    def normalize_scope(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def validate_source_channel(self) -> "SourceVersionCommand":
        if (
            self.source_category == SourceCategory.PRACTITIONER
            and self.knowledge_origin == KnowledgeOrigin.SCIENTIFIC_EVIDENCE
        ):
            raise ValueError("practitioner sources cannot claim scientific evidence origin")
        if (
            self.requested_extraction_permission == ExtractionPermission.SELECTED_SECTIONS
            and not self.requested_scope_locators
        ):
            raise ValueError("selected_sections requires requested_scope_locators")
        return self


class SourcePermissionRequestCommand(BaseModel):
    """Append-only request to change the review ceiling of one Source version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["source-permission-request-0.1"] = (
        SOURCE_PERMISSION_REQUEST_SCHEMA_VERSION
    )
    source_version_id: UUID
    requested_extraction_permission: ExtractionPermission = ExtractionPermission.NONE
    requested_storage_permission: StoragePermission = StoragePermission.NONE
    requested_scope_locators: list[str] = Field(default_factory=list, max_length=500)
    rights_basis: str = Field(min_length=1, max_length=2_000)
    rights_evidence: list[str] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2_000)

    @field_validator("requested_scope_locators", "rights_evidence")
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("rights_basis", "reason")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("permission request text must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_permission_shape(self) -> "SourcePermissionRequestCommand":
        if not self.rights_evidence:
            raise ValueError("rights_evidence must contain at least one item")
        selected = (
            self.requested_extraction_permission
            == ExtractionPermission.SELECTED_SECTIONS
        )
        if selected and not self.requested_scope_locators:
            raise ValueError("selected_sections requires requested_scope_locators")
        if not selected and self.requested_scope_locators:
            raise ValueError(
                "requested_scope_locators are only valid for selected_sections"
            )
        if (
            self.requested_storage_permission != StoragePermission.NONE
            and self.requested_extraction_permission
            not in {
                ExtractionPermission.SELECTED_SECTIONS,
                ExtractionPermission.FULL_TEXT,
            }
        ):
            raise ValueError(
                "storage permission requires selected_sections or full_text extraction"
            )
        return self


class SourceReviewCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_version_id: UUID
    source_permission_request_id: UUID
    decision: ReviewDecision
    reason: str = Field(min_length=1, max_length=2_000)
    claim_eligibility: ClaimEligibility = ClaimEligibility.INELIGIBLE
    extraction_permission: ExtractionPermission = ExtractionPermission.NONE
    extraction_scope_locators: list[str] = Field(default_factory=list, max_length=500)
    storage_permission: StoragePermission = StoragePermission.NONE
    sensitive_information_level: SensitiveInformationLevel = (
        SensitiveInformationLevel.CONTROLLED
    )
    contradicting_evidence_checked: ContradictionCheckStatus = (
        ContradictionCheckStatus.NOT_APPLICABLE
    )
    citation_verification: list[CitationVerificationInput] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Independent reviewer observations used to derive citation status; "
            "registration-time assertions alone cannot verify a citation."
        ),
    )

    @field_validator("extraction_scope_locators")
    @classmethod
    def normalize_scope(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("review reason must not be blank")
        return normalized


class ExtractionProvenanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    producer: ExtractionProducer
    extractor: str = Field(min_length=1, max_length=200)
    extraction_schema_version: str = Field(min_length=1, max_length=100)
    run_id: str | None = Field(default=None, max_length=255)
    llm_provider: Literal["GLM"] | None = None
    model: str | None = Field(default=None, max_length=200)
    tool_version: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def keep_glm_as_only_llm(self) -> "ExtractionProvenanceInput":
        if self.producer == ExtractionProducer.GLM:
            if self.llm_provider != "GLM" or not self.model or not self.run_id:
                raise ValueError("GLM extraction requires provider, model, and run_id")
        elif self.llm_provider is not None or self.model is not None:
            raise ValueError("LLM metadata is only valid for GLM-produced candidates")
        return self


class ClaimCandidateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["claim-candidate-0.1"] = CLAIM_CANDIDATE_SCHEMA_VERSION
    source_version_id: UUID
    claim: str = Field(min_length=1, max_length=8_000)
    claim_role: ClaimRole
    claim_polarity: ClaimPolarity = ClaimPolarity.SUPPORTS
    proposed_evidence_class: EvidenceClass
    applicable_domain: list[MagicDomain] = Field(min_length=1, max_length=10)
    ontology_paths: list[str] = Field(min_length=1, max_length=100)
    topic_tags: list[str] = Field(default_factory=list, max_length=100)
    mechanism_ids: list[UUID] = Field(default_factory=list, max_length=100)
    mechanism_status: MechanismStatus = MechanismStatus.UNRESOLVED
    principle_ids: list[UUID] = Field(default_factory=list, max_length=100)
    magic_application: str | None = Field(default=None, max_length=8_000)
    application_origin: ApplicationOrigin = ApplicationOrigin.NOT_APPLICABLE
    locator: EvidenceLocator
    evidence_excerpt: str = Field(min_length=1, max_length=1_000)
    proposed_limitations: list[str] = Field(min_length=1, max_length=100)
    population_context: str | None = Field(default=None, max_length=4_000)
    performance_context: str | None = Field(default=None, max_length=4_000)
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    extraction_provenance: ExtractionProvenanceInput

    @field_validator("claim")
    @classmethod
    def normalize_claim(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("ontology_paths")
    @classmethod
    def normalize_paths(cls, values: list[str]) -> list[str]:
        return normalize_ontology_paths(values)

    @field_validator("topic_tags", "proposed_limitations")
    @classmethod
    def normalize_text_lists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def enforce_evidence_contract(self) -> "ClaimCandidateCommand":
        validate_claim_role_class(self.claim_role, self.proposed_evidence_class)
        validate_claim_fidelity(
            self.claim,
            self.evidence_excerpt,
            claim_role=self.claim_role,
        )
        if self.mechanism_ids and self.mechanism_status != MechanismStatus.LINKED:
            raise ValueError("mechanism_ids require linked mechanism status")
        if self.mechanism_status == MechanismStatus.LINKED and not self.mechanism_ids:
            raise ValueError("linked mechanism status requires mechanism_ids")
        if self.magic_application and self.application_origin == ApplicationOrigin.NOT_APPLICABLE:
            raise ValueError("magic_application requires application_origin")
        if not self.magic_application and self.application_origin != ApplicationOrigin.NOT_APPLICABLE:
            raise ValueError("application_origin requires magic_application")
        return self


class ClaimReviewCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: ReviewDecision
    reason: str = Field(min_length=1, max_length=2_000)
    confidence: ConfidenceAssessment | None = None
    claim_eligibility: ClaimEligibility = ClaimEligibility.INELIGIBLE
    storage_permission: StoragePermission = StoragePermission.NONE
    sensitive_information_level: SensitiveInformationLevel = (
        SensitiveInformationLevel.CONTROLLED
    )
    contradiction_status: ContradictionStatus = ContradictionStatus.NOT_CHECKED
    contradicting_evidence_checked: ContradictionCheckStatus = (
        ContradictionCheckStatus.NOT_CHECKED
    )
    contradicting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    limitations: list[str] = Field(default_factory=list, max_length=100)
    secret_exposure_level: SecretExposureLevel = SecretExposureLevel.GENERAL_PRINCIPLE

    @field_validator("limitations")
    @classmethod
    def normalize_limitations(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("review reason must not be blank")
        return normalized


class SourceVersionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    version: int
    content_hash: str
    content_access: ContentAccess
    source_type: SourceType
    knowledge_origin: KnowledgeOrigin
    sensitivity: SensitiveInformationLevel
    citation_status: str
    status: WorkflowStatus
    latest_permission_request_id: UUID | None = None
    created_at: datetime


class SourceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    canonical_key: str
    title: str
    source_category: SourceCategory
    versions: list[SourceVersionSummary]


class CitationVerificationEvidenceView(BaseModel):
    """Reviewer-only view of one immutable citation verification observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    scope: CitationVerificationScope
    method: str
    resolver_result: ResolverResult
    resolver_name: str
    verified_identifier: str
    checked_locator: str
    review_actor: str
    review_timestamp: datetime
    evidence_checksum: str
    actor_role_snapshot: list[str]
    trusted_for_status: bool
    notes: str


class SourcePermissionRequestView(BaseModel):
    """Reviewer-safe representation of one immutable permission request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    source_version_id: UUID
    sequence: int
    requested_extraction_permission: ExtractionPermission
    requested_storage_permission: StoragePermission
    requested_scope_locators: list[str]
    rights_basis: str
    rights_evidence: list[str]
    reason: str
    submitted_by: str
    actor_role_snapshot: list[str]
    request_checksum: str
    supersedes_request_id: UUID | None = None
    created_at: datetime


class SourceVersionReviewView(SourceVersionSummary):
    """Clearance-filtered material required for an actual Source review."""

    citation: CitationMetadataInput
    access: SourceAccessMetadata
    requested_extraction_permission: ExtractionPermission
    requested_storage_permission: StoragePermission
    requested_scope_locators: list[str]
    content: str
    citation_verification: list[CitationVerificationEvidenceView]
    latest_permission_request: SourcePermissionRequestView | None = None
    permission_requests: list[SourcePermissionRequestView] = Field(default_factory=list)


class SourceReviewDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    canonical_key: str
    title: str
    source_category: SourceCategory
    versions: list[SourceVersionReviewView]


class SourceReviewQueueItem(BaseModel):
    """Lightweight Source-version envelope for reviewer queue discovery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: UUID
    source_version_id: UUID
    version: int
    canonical_key: str
    title: str
    source_category: SourceCategory
    source_type: SourceType
    knowledge_origin: KnowledgeOrigin
    content_access: ContentAccess
    citation_status: str
    status: WorkflowStatus
    latest_permission_request_id: UUID | None = None
    sensitivity: SensitiveInformationLevel
    created_at: datetime


class ClaimCandidateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    source_version_id: UUID
    claim: str
    claim_role: ClaimRole
    proposed_evidence_class: EvidenceClass
    status: WorkflowStatus
    candidate_checksum: str
    sensitivity: SensitiveInformationLevel
    submitted_at: datetime


class ClaimSourceContextView(BaseModel):
    """Clearance-filtered exact Source context required for Claim review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: UUID
    source_version_id: UUID
    version: int
    title: str
    source_category: SourceCategory
    citation: CitationMetadataInput
    access: SourceAccessMetadata
    content: str
    content_access: ContentAccess
    source_type: SourceType
    knowledge_origin: KnowledgeOrigin
    sensitivity: SensitiveInformationLevel
    citation_status: str


class ClaimCandidateReviewView(ClaimCandidateSummary):
    """Reviewer-only, source-bound Claim material; no private prompt is stored."""

    claim_polarity: ClaimPolarity
    applicable_domain: list[MagicDomain]
    ontology_paths: list[str]
    topic_tags: list[str]
    mechanism_ids: list[UUID]
    mechanism_status: MechanismStatus
    principle_ids: list[UUID]
    magic_application: str | None
    application_origin: ApplicationOrigin
    locator: EvidenceLocator
    evidence_excerpt: str
    proposed_limitations: list[str]
    population_context: str | None
    performance_context: str | None
    extraction_confidence: float
    extraction_provenance: ExtractionProvenanceInput
    candidate_schema_version: str
    source_context: ClaimSourceContextView


class ClaimReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim: ClaimCandidateSummary
    evidence_card_id: UUID | None = None
    evidence_version_id: UUID | None = None


class EvidenceVersionView(BaseModel):
    """Public-safe view; reviewer identity and private decision notes are absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_card_id: UUID
    evidence_version_id: UUID
    version: int
    schema_version: str
    claim: str
    claim_role: ClaimRole
    evidence_class: EvidenceClass
    knowledge_origin: KnowledgeOrigin
    applicable_domain: list[MagicDomain]
    ontology_paths: list[str]
    source_type: SourceType
    source_year: int | None
    citation_id: UUID
    source_locator: str | None = None
    evidence_excerpt: str | None = None
    limitations: list[str]
    confidence_score: float
    confidence_label: str
    contradiction_status: ContradictionStatus
    sensitivity: SensitiveInformationLevel
    secret_exposure_level: SecretExposureLevel
    created_at: datetime


__all__ = [
    "CLAIM_CANDIDATE_SCHEMA_VERSION",
    "CitationMetadataInput",
    "CitationVerificationInput",
    "CitationVerificationEvidenceView",
    "CitationVerificationMethod",
    "CitationVerificationScope",
    "ClaimCandidateCommand",
    "ClaimCandidateReviewView",
    "ClaimCandidateSummary",
    "ClaimReviewCommand",
    "ClaimReviewResult",
    "ClaimSourceContextView",
    "EvidenceVersionView",
    "ExtractionProducer",
    "ExtractionProvenanceInput",
    "REVIEW_POLICY_VERSION",
    "ResolverResult",
    "ReviewDecision",
    "SOURCE_PERMISSION_REQUEST_SCHEMA_VERSION",
    "SOURCE_WORKFLOW_SCHEMA_VERSION",
    "SourceAccessMetadata",
    "SourcePermissionRequestCommand",
    "SourcePermissionRequestView",
    "SourceReviewCommand",
    "SourceReviewDetail",
    "SourceReviewQueueItem",
    "SourceSummary",
    "SourceVersionCommand",
    "SourceVersionReviewView",
    "SourceVersionSummary",
    "WorkflowStatus",
]
