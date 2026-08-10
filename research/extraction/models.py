"""Evidence-first structured output models for approved-source extraction."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from knowledge.models import EntityType, KnowledgeEntity, RelationType
from research.citation.models import CitationRecord
from knowledge.evidence import (
    ApplicationOrigin,
    ClaimPolarity,
    ClaimRole,
    EvidenceCard,
    EvidenceClass,
    MagicDomain,
    validate_claim_fidelity,
    validate_claim_role_class,
)
from knowledge.ontology import normalize_ontology_paths
from research.models import ResearchCandidate
from research.review.source_models import SourceApprovalRecord


EXTRACTION_SCHEMA_VERSION = "extraction-0.2"


class ExtractedEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EntityType
    name: str = Field(min_length=1)
    description: str | None = None
    evidence_excerpt: str = Field(min_length=1, max_length=1_000)
    supporting_claims: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def reserve_provenance_entities(self) -> "ExtractedEntity":
        if self.type in {EntityType.SOURCE, EntityType.RESEARCH_PAPER}:
            raise ValueError("Source and Research Paper nodes are created from verified provenance")
        return self


class ExtractedRelationship(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_name: str = Field(min_length=1)
    target_name: str = Field(min_length=1)
    type: RelationType
    evidence_excerpt: str = Field(min_length=1, max_length=1_000)
    supporting_claims: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractedClaim(BaseModel):
    """GLM proposal only; confidence is extraction fidelity, never evidence strength."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    evidence_excerpt: str = Field(min_length=1, max_length=1_000)
    locator: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    claim_role: ClaimRole
    evidence_class: EvidenceClass
    claim_polarity: ClaimPolarity = ClaimPolarity.SUPPORTS
    applicable_domains: list[MagicDomain] = Field(min_length=1)
    ontology_paths: list[str] = Field(min_length=1)
    topic_tags: list[str] = Field(default_factory=list)
    mechanism_names: list[str] = Field(default_factory=list)
    principle_names: list[str] = Field(default_factory=list)
    magic_application: str | None = None
    application_origin: ApplicationOrigin = ApplicationOrigin.NOT_APPLICABLE
    limitations: list[str] = Field(min_length=1)
    population_context: str | None = None
    performance_context: str | None = None

    @field_validator("statement")
    @classmethod
    def normalize_statement(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("ontology_paths")
    @classmethod
    def normalize_paths(cls, values: list[str]) -> list[str]:
        return normalize_ontology_paths(values)

    @model_validator(mode="after")
    def validate_application(self) -> "ExtractedClaim":
        validate_claim_role_class(self.claim_role, self.evidence_class)
        validate_claim_fidelity(
            self.statement,
            self.evidence_excerpt,
            claim_role=self.claim_role,
        )
        if self.application_origin == ApplicationOrigin.REVIEWER_SYNTHESIS:
            raise ValueError(
                "GLM extraction cannot claim reviewer_synthesis authorship"
            )
        if self.magic_application and self.application_origin == ApplicationOrigin.NOT_APPLICABLE:
            raise ValueError("magic_application requires application_origin")
        if not self.magic_application and self.application_origin != ApplicationOrigin.NOT_APPLICABLE:
            raise ValueError("application_origin requires magic_application")
        if self.magic_application and self.magic_application.casefold().strip() in {
            "n/a",
            "none",
            "not applicable",
            "not specified",
            ApplicationOrigin.NOT_APPLICABLE.value,
            ApplicationOrigin.REVIEWER_SYNTHESIS.value,
            ApplicationOrigin.SOURCE_STATED.value,
        }:
            raise ValueError(
                "magic_application must describe an application, not an enum or placeholder"
            )
        return self


class ResearchExtractionResult(BaseModel):
    """Validated GLM contract for exactly one temporary source chunk."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = EXTRACTION_SCHEMA_VERSION
    magic_category: str = ""
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relationships: list[ExtractedRelationship] = Field(default_factory=list)
    claims: list[ExtractedClaim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)

    @field_validator("limitations", "conflicts")
    @classmethod
    def remove_blank_notes(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value.strip()]

    @model_validator(mode="after")
    def require_mapping_claim_links(self) -> "ResearchExtractionResult":
        claims = {claim.statement.casefold(): claim for claim in self.claims}
        for proposal in [*self.entities, *self.relationships]:
            unknown = {
                statement
                for statement in proposal.supporting_claims
                if " ".join(statement.split()).casefold() not in claims
            }
            if unknown:
                raise ValueError(
                    "entity/relationship supporting_claims must reference extracted claims: "
                    + ", ".join(sorted(unknown))
                )
            context_only = {
                statement
                for statement in proposal.supporting_claims
                if claims[" ".join(statement.split()).casefold()].claim_role
                == ClaimRole.CONTEXT_ONLY
            }
            if context_only:
                raise ValueError(
                    "context_only claims cannot support entities or relationships: "
                    + ", ".join(sorted(context_only))
                )
        return self


class EntityMappingProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    entity: KnowledgeEntity
    evidence_excerpt: str = Field(min_length=1, max_length=1_000)
    source_locator: str = Field(min_length=1)
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence_card_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def assign_id(self) -> "EntityMappingProposal":
        for card_id in self.supporting_evidence_card_ids:
            UUID(card_id)
        identity = ":".join(
            (
                self.entity.id,
                self.source_locator,
                *sorted(self.supporting_evidence_card_ids),
            )
        )
        proposal_id = str(uuid5(NAMESPACE_URL, f"magicforge:entity-mapping:{identity}"))
        if self.id and self.id != proposal_id:
            raise ValueError("entity mapping ID does not match contents")
        object.__setattr__(self, "id", proposal_id)
        return self


class RelationshipMappingProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    source_entity_id: str
    target_entity_id: str
    type: RelationType
    assertion: str = Field(min_length=1)
    evidence_excerpt: str = Field(min_length=1, max_length=1_000)
    source_locator: str = Field(min_length=1)
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence_card_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def assign_id(self) -> "RelationshipMappingProposal":
        UUID(self.source_entity_id)
        UUID(self.target_entity_id)
        for card_id in self.supporting_evidence_card_ids:
            UUID(card_id)
        identity = ":".join(
            (
                self.source_entity_id,
                self.type.value,
                self.target_entity_id,
                self.assertion.casefold(),
                *sorted(self.supporting_evidence_card_ids),
            )
        )
        proposal_id = str(
            uuid5(NAMESPACE_URL, f"magicforge:relationship-mapping:{identity}")
        )
        if self.id and self.id != proposal_id:
            raise ValueError("relationship mapping ID does not match contents")
        object.__setattr__(self, "id", proposal_id)
        return self


class ChunkProposalArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_cards: list[EvidenceCard] = Field(default_factory=list)
    entity_proposals: list[EntityMappingProposal] = Field(default_factory=list)
    relationship_proposals: list[RelationshipMappingProposal] = Field(default_factory=list)


class KnowledgeProposal(BaseModel):
    """Review candidates only; raw chunks and storage projections are prohibited."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    schema_version: str = EXTRACTION_SCHEMA_VERSION
    extraction_run_id: str
    candidate: ResearchCandidate
    citation: CitationRecord
    source_approval: SourceApprovalRecord
    provenance_entities: list[KnowledgeEntity] = Field(min_length=1)
    context_claims: list[ExtractedClaim] = Field(default_factory=list)
    evidence_cards: list[EvidenceCard] = Field(default_factory=list)
    entity_proposals: list[EntityMappingProposal] = Field(default_factory=list)
    relationship_proposals: list[RelationshipMappingProposal] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    temporary_chunk_count: int = Field(ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def assign_id(self) -> "KnowledgeProposal":
        UUID(self.extraction_run_id)
        if self.citation.source_candidate_id != self.candidate.id:
            raise ValueError("citation must refer to the proposal source candidate")
        if self.source_approval.source_candidate_id != self.candidate.id:
            raise ValueError("source approval must refer to the proposal candidate")
        if self.source_approval.citation_id != self.citation.id:
            raise ValueError("source approval must refer to the proposal citation")
        identity = ":".join(
            (
                self.extraction_run_id,
                self.source_approval.id,
                *(sorted(claim.statement.casefold() for claim in self.context_claims)),
                *sorted(card.id for card in self.evidence_cards),
                *sorted(proposal.id for proposal in self.entity_proposals),
                *sorted(proposal.id for proposal in self.relationship_proposals),
            )
        )
        proposal_id = str(uuid5(NAMESPACE_URL, f"magicforge:proposal:{identity}"))
        if self.id and self.id != proposal_id:
            raise ValueError("knowledge proposal ID does not match contents")
        object.__setattr__(self, "id", proposal_id)
        return self
