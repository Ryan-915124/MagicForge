"""Production Mapping workflow contracts for Checkpoint 4.

These models describe immutable proposals and human review commands.  They do
not authorize storage and never accept a caller supplied review status.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from knowledge.evidence import ConfidenceAssessment, KnowledgeOrigin, MagicDomain
from knowledge.models import EntityType, KnowledgeEntity, RelationType
from knowledge.ontology import normalize_ontology_paths
from knowledge.projections import KnowledgeNodeVersion, KnowledgeRelationshipAssertion
from research.review.workflow_models import ReviewDecision, WorkflowStatus


MAPPING_PROPOSAL_SCHEMA_VERSION = "mapping-proposal-0.1"
MAPPING_REVIEW_POLICY_VERSION = "mapping-review-policy-0.1"
ENTITY_VALIDATION_RULE_VERSION = "entity-validation-0.2"
RELATIONSHIP_ENTAILMENT_RULE_VERSION = "relationship-entailment-0.2"


class MappingProposalKind(StrEnum):
    ENTITY = "entity"
    RELATIONSHIP = "relationship"


class CanonicalResolution(StrEnum):
    CREATE = "create"
    REUSE = "reuse"
    MERGE = "merge"


class EntityMappingCommand(BaseModel):
    """Complete Knowledge Node proposal over exact Evidence Card versions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["mapping-proposal-0.1"] = MAPPING_PROPOSAL_SCHEMA_VERSION
    entity: KnowledgeEntity
    definition: str = Field(min_length=1, max_length=20_000)
    domains: list[MagicDomain] = Field(min_length=1)
    ontology_paths: list[str] = Field(min_length=1)
    topic_tags: list[str] = Field(default_factory=list)
    knowledge_origin: KnowledgeOrigin
    evidence_excerpt: str = Field(min_length=1, max_length=1_000)
    source_locator: str = Field(min_length=1, max_length=4_000)
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence_version_ids: list[UUID] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    proposer_run_id: str | None = Field(default=None, max_length=255)
    supersedes_proposal_id: UUID | None = None

    @field_validator("definition", "evidence_excerpt", "source_locator")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("mapping proposal text cannot be blank")
        return normalized

    @field_validator("ontology_paths")
    @classmethod
    def normalize_paths(cls, values: list[str]) -> list[str]:
        return normalize_ontology_paths(values)

    @field_validator("topic_tags", "limitations")
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.split()) for value in values if value.strip()]
        return list(dict.fromkeys(normalized))

    @field_validator("supporting_evidence_version_ids")
    @classmethod
    def unique_evidence_versions(cls, values: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(values))

    @field_validator("proposer_run_id")
    @classmethod
    def normalize_run(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def require_normalized_content(self) -> "EntityMappingCommand":
        if not self.limitations:
            raise ValueError("mapping proposal requires explicit limitations")
        return self


class RelationshipMappingCommand(BaseModel):
    """Complete relationship assertion proposal over canonical endpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["mapping-proposal-0.1"] = MAPPING_PROPOSAL_SCHEMA_VERSION
    source_entity_id: UUID
    target_entity_id: UUID
    relation_type: RelationType
    assertion: str = Field(min_length=1, max_length=20_000)
    domains: list[MagicDomain] = Field(min_length=1)
    ontology_paths: list[str] = Field(min_length=1)
    topic_tags: list[str] = Field(default_factory=list)
    knowledge_origin: KnowledgeOrigin
    evidence_excerpt: str = Field(min_length=1, max_length=1_000)
    source_locator: str = Field(min_length=1, max_length=4_000)
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence_version_ids: list[UUID] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    proposer_run_id: str | None = Field(default=None, max_length=255)
    supersedes_proposal_id: UUID | None = None

    @field_validator("assertion", "evidence_excerpt", "source_locator")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("mapping proposal text cannot be blank")
        return normalized

    @field_validator("ontology_paths")
    @classmethod
    def normalize_paths(cls, values: list[str]) -> list[str]:
        return normalize_ontology_paths(values)

    @field_validator("topic_tags", "limitations")
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(value.split()) for value in values if value.strip()]
        return list(dict.fromkeys(normalized))

    @field_validator("supporting_evidence_version_ids")
    @classmethod
    def unique_evidence_versions(cls, values: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(values))

    @field_validator("proposer_run_id")
    @classmethod
    def normalize_run(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def reject_self_relation(self) -> "RelationshipMappingCommand":
        if self.source_entity_id == self.target_entity_id:
            raise ValueError("relationship endpoints must be different")
        if not self.limitations:
            raise ValueError("mapping proposal requires explicit limitations")
        return self


class MappingReviewCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: ReviewDecision
    reason: str = Field(min_length=1, max_length=4_000)
    confidence: ConfidenceAssessment | None = None
    canonical_resolution: CanonicalResolution | None = None
    canonical_entity_id: UUID | None = None

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("mapping review reason is required")
        return normalized

    @model_validator(mode="after")
    def validate_decision_shape(self) -> "MappingReviewCommand":
        if self.decision == ReviewDecision.APPROVE:
            if self.confidence is None:
                raise ValueError("mapping approval requires confidence")
        elif self.confidence is not None:
            raise ValueError("mapping rejection cannot carry confidence")
        if self.canonical_resolution in {
            CanonicalResolution.REUSE,
            CanonicalResolution.MERGE,
        } and self.canonical_entity_id is None:
            raise ValueError("reuse/merge requires canonical_entity_id")
        if self.canonical_resolution == CanonicalResolution.CREATE and self.canonical_entity_id:
            raise ValueError("create cannot specify canonical_entity_id")
        return self


class ValidationRunView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    id: UUID
    phase: str
    rule_version: str
    passed: bool
    results: dict[str, object]
    created_at: datetime


class MappingProposalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    kind: MappingProposalKind
    status: WorkflowStatus
    schema_version: str
    proposal_checksum: str
    subject: str
    proposer_user_id: UUID | None
    proposer_run_id: str | None
    sensitivity: str
    submitted_at: datetime
    approved_artifact_id: UUID | None = None
    approved_artifact_version: int | None = None


class MappingProposalDetail(MappingProposalSummary):
    proposal: EntityMappingCommand | RelationshipMappingCommand
    supporting_evidence_version_ids: list[UUID]
    validation_runs: list[ValidationRunView] = Field(default_factory=list)


class MappingReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mapping: MappingProposalSummary
    knowledge_node_version_id: UUID | None = None
    relationship_assertion_id: UUID | None = None

    @model_validator(mode="after")
    def enforce_artifact_shape(self) -> "MappingReviewResult":
        artifacts = [
            value
            for value in (
                self.knowledge_node_version_id,
                self.relationship_assertion_id,
            )
            if value is not None
        ]
        if self.mapping.status == WorkflowStatus.APPROVED:
            if len(artifacts) != 1:
                raise ValueError("approved Mapping result requires exactly one artifact")
            if (
                self.mapping.kind == MappingProposalKind.ENTITY
                and self.knowledge_node_version_id is None
            ) or (
                self.mapping.kind == MappingProposalKind.RELATIONSHIP
                and self.relationship_assertion_id is None
            ):
                raise ValueError("approved artifact does not match Mapping kind")
        elif artifacts:
            raise ValueError("unapproved Mapping result cannot expose an artifact")
        return self


class CanonicalEntitySummary(BaseModel):
    """Active SQL registry entry safe for Mapping canonical resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    entity_type: EntityType
    canonical_name: str
    canonical_key: str
    aliases: list[str] = Field(default_factory=list)
    description: str
    status: Literal["active"] = "active"
    latest_knowledge_node_version_id: UUID
    latest_version: int


class KnowledgeNodeVersionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_id: UUID
    entity_id: UUID
    version: int
    payload_checksum: str
    node: KnowledgeNodeVersion
    created_at: datetime


class KnowledgeRelationshipAssertionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_id: UUID
    relationship_id: UUID
    version: int
    payload_checksum: str
    assertion: KnowledgeRelationshipAssertion
    domains: list[MagicDomain]
    ontology_paths: list[str]
    topic_tags: list[str]
    created_at: datetime


__all__ = [
    "CanonicalResolution",
    "CanonicalEntitySummary",
    "ENTITY_VALIDATION_RULE_VERSION",
    "EntityMappingCommand",
    "KnowledgeNodeVersionView",
    "KnowledgeRelationshipAssertionView",
    "MAPPING_PROPOSAL_SCHEMA_VERSION",
    "MAPPING_REVIEW_POLICY_VERSION",
    "MappingProposalDetail",
    "MappingProposalKind",
    "MappingProposalSummary",
    "MappingReviewCommand",
    "MappingReviewResult",
    "RELATIONSHIP_ENTAILMENT_RULE_VERSION",
    "RelationshipMappingCommand",
    "ValidationRunView",
]
