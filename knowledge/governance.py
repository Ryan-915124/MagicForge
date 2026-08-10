"""Storage-neutral governance vocabulary for evidence and knowledge."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    INGESTED = "ingested"
    BOOTSTRAP_PENDING_HUMAN_REVIEW = "bootstrap_pending_human_review"
    BOOTSTRAP_GENERATED = "bootstrap_generated"
    BOOTSTRAP = "bootstrap"


class MagicForgeMode(StrEnum):
    """Runtime policy mode; production remains the fail-closed default."""

    PRODUCTION = "production"
    BOOTSTRAP = "bootstrap"


class ClaimEligibility(StrEnum):
    NOT_ASSESSED = "not_assessed"
    ELIGIBLE = "eligible"
    ELIGIBLE_WITH_LIMITS = "eligible_with_limits"
    INELIGIBLE = "ineligible"

    @property
    def allows_projection(self) -> bool:
        return self in {self.ELIGIBLE, self.ELIGIBLE_WITH_LIMITS}


class ExtractionPermission(StrEnum):
    NONE = "none"
    METADATA_ONLY = "metadata_only"
    SELECTED_SECTIONS = "selected_sections"
    FULL_TEXT = "full_text"

    @property
    def allows_claim_extraction(self) -> bool:
        return self in {self.SELECTED_SECTIONS, self.FULL_TEXT}


class StoragePermission(StrEnum):
    NONE = "none"
    DERIVED_KNOWLEDGE_ONLY = "derived_knowledge_only"
    DERIVED_WITH_SHORT_EXCERPT = "derived_with_short_excerpt"

    @property
    def allows_projection(self) -> bool:
        return self in {
            self.DERIVED_KNOWLEDGE_ONLY,
            self.DERIVED_WITH_SHORT_EXCERPT,
        }


class SensitiveInformationLevel(StrEnum):
    PUBLIC = "public"
    CONTROLLED = "controlled"
    SECRET_METHOD = "secret_method"
    RESTRICTED = "restricted"


class ContradictionCheckStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    CHECKED_NONE_FOUND = "checked_none_found"
    CHECKED_CONFLICTS_LINKED = "checked_conflicts_linked"
    NOT_APPLICABLE = "not_applicable"

    @property
    def completed(self) -> bool:
        return self != self.NOT_CHECKED


class SecretExposureLevel(StrEnum):
    NONE = "none"
    GENERAL_PRINCIPLE = "general_principle"
    METHOD_DETAIL = "method_detail"
    OPERATIONAL_SECRET = "operational_secret"

    @property
    def rank(self) -> int:
        return {
            self.NONE: 0,
            self.GENERAL_PRINCIPLE: 1,
            self.METHOD_DETAIL: 2,
            self.OPERATIONAL_SECRET: 3,
        }[self]


class ClearanceRank(IntEnum):
    PUBLIC = 0
    GENERAL_PRINCIPLE = 1
    METHOD_DETAIL = 2
    OPERATIONAL_SECRET = 3


class ReviewEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    notes: str = ""
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


_AUTOMATED_ACTORS = {
    "system",
    "automation",
    "research-pipeline",
    "llm",
    "glm",
}


def require_human_identity(value: str) -> str:
    """Return a normalized named-human identity or fail closed."""

    actor = value.strip()
    if not actor or actor.casefold() in _AUTOMATED_ACTORS:
        raise ValueError("a named human reviewer/editor is required")
    return actor
