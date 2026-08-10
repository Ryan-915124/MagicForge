"""Version-bound source approval records for the first human review gate."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research.citation.models import CitationStatus
from knowledge.evidence import SourceType
from knowledge.governance import (
    ClaimEligibility,
    ContradictionCheckStatus,
    ExtractionPermission,
    ReviewStatus,
    SensitiveInformationLevel,
    StoragePermission,
    ReviewEvent,
)
from research.models import ContentAccess


class ExtractionScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    locators: list[str] = Field(default_factory=list)
    notes: str = ""


class SourceApprovalRecord(BaseModel):
    """A human decision about whether an exact source version may be extracted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    source_candidate_id: str
    citation_id: str
    citation_status: CitationStatus
    source_version_id: str = ""
    source_permission_request_id: str = ""
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_access: ContentAccess
    source_type: SourceType
    extraction_scope: ExtractionScope = Field(default_factory=ExtractionScope)
    status: ReviewStatus = ReviewStatus.PENDING
    reviewer: str | None = None
    review_date: datetime | None = None
    approval_reason: str | None = None
    claim_eligibility: ClaimEligibility = ClaimEligibility.NOT_ASSESSED
    extraction_permission: ExtractionPermission = ExtractionPermission.NONE
    storage_permission: StoragePermission = StoragePermission.NONE
    sensitive_information_level: SensitiveInformationLevel = (
        SensitiveInformationLevel.CONTROLLED
    )
    contradicting_evidence_checked: ContradictionCheckStatus = (
        ContradictionCheckStatus.NOT_APPLICABLE
    )
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    audit_log: list[ReviewEvent] = Field(default_factory=list)

    @field_validator(
        "source_candidate_id",
        "citation_id",
        "source_version_id",
        "source_permission_request_id",
    )
    @classmethod
    def validate_ids(cls, value: str) -> str:
        if value:
            UUID(value)
        return value

    @model_validator(mode="after")
    def assign_identity_and_validate(self) -> "SourceApprovalRecord":
        source_version_id = str(
            uuid5(
                NAMESPACE_URL,
                f"magicforge:source-version:{self.source_candidate_id}:{self.content_hash}",
            )
        )
        if self.source_version_id and self.source_version_id != source_version_id:
            raise ValueError("source_version_id does not match candidate/content hash")
        object.__setattr__(self, "source_version_id", source_version_id)
        record_id = str(
            uuid5(NAMESPACE_URL, f"magicforge:source-approval:{source_version_id}")
        )
        if self.id and self.id != record_id:
            raise ValueError("source approval ID does not match source version")
        object.__setattr__(self, "id", record_id)

        if (
            self.extraction_permission == ExtractionPermission.SELECTED_SECTIONS
            and not self.extraction_scope.locators
        ):
            raise ValueError("selected_sections permission requires explicit locators")
        if self.status == ReviewStatus.INGESTED:
            raise ValueError("source approval records cannot have ingested status")
        if self.status == ReviewStatus.APPROVED:
            if not self.reviewer or not self.review_date or not self.approval_reason:
                raise ValueError("approved source requires reviewer, date, and reason")
            if not self.claim_eligibility.allows_projection:
                raise ValueError("approved source must be eligible for claim extraction")
            if not self.extraction_permission.allows_claim_extraction:
                raise ValueError("approved source must permit claim extraction")
            if self.citation_status not in {
                CitationStatus.METADATA_VERIFIED,
                CitationStatus.FULL_TEXT_VERIFIED,
            }:
                raise ValueError("approved source requires a verified citation")
            if self.content_access in {
                ContentAccess.SEARCH_SNIPPET,
                ContentAccess.ABSTRACT,
            }:
                raise ValueError("approved source requires acquired claim-level content")
        return self

    @property
    def allows_claim_extraction(self) -> bool:
        return bool(
            self.status == ReviewStatus.APPROVED
            and self.claim_eligibility.allows_projection
            and self.extraction_permission.allows_claim_extraction
        )

    @property
    def allows_bootstrap_extraction(self) -> bool:
        return bool(
            self.status == ReviewStatus.BOOTSTRAP_PENDING_HUMAN_REVIEW
            and self.claim_eligibility.allows_projection
            and self.extraction_permission.allows_claim_extraction
        )
