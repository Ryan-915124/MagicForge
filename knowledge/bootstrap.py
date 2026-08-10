"""Isolated, explicitly unverified artifacts for one-time corpus bootstrapping.

Production projections and manifests intentionally remain unusable here.  Every
payload emitted by this module is visibly machine-generated, unapproved, and
restricted to the dedicated bootstrap collection.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from knowledge.evidence import (
    ClaimRole,
    ConfidenceAssessment,
    ConfidenceLabel,
    ContradictionStatus,
    EvidenceCard,
    EvidenceClass,
    EvidenceLevel,
    EvidenceLocator,
    KnowledgeOrigin,
    MagicDomain,
    SourceType,
)
from knowledge.governance import (
    ReviewStatus,
    SecretExposureLevel,
    SensitiveInformationLevel,
)
from knowledge.models import EntityType, KnowledgeEntity, KnowledgeRelationship
from knowledge.ontology import normalize_ontology_paths
from knowledge.projections import ArtifactType, KnowledgeType


BOOTSTRAP_COLLECTION_NAME = "magicforge_bootstrap_v02"
BOOTSTRAP_PROJECTION_SCHEMA_VERSION = "bootstrap-qdrant-0.2"
BOOTSTRAP_MANIFEST_SCHEMA_VERSION = "bootstrap-manifest-0.2"


class BootstrapKnowledgeNodeCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    entity: KnowledgeEntity
    definition: str = Field(min_length=1)
    domains: list[MagicDomain] = Field(min_length=1)
    ontology_paths: list[str] = Field(min_length=1)
    topic_tags: list[str] = Field(default_factory=list)
    knowledge_origin: KnowledgeOrigin
    supporting_evidence_ids: list[str] = Field(min_length=1)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(min_length=1)
    confidence: ConfidenceAssessment
    verification_status: Literal["unverified"] = "unverified"
    bootstrap_generated: Literal[True] = True
    human_verified: Literal[False] = False
    created_by: Literal["glm", "demo-seed"] = "glm"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("supporting_evidence_ids", "contradicting_evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: list[str]) -> list[str]:
        for value in values:
            UUID(value)
        return list(dict.fromkeys(values))

    @field_validator("ontology_paths")
    @classmethod
    def normalize_paths(cls, values: list[str]) -> list[str]:
        return normalize_ontology_paths(values)

    @model_validator(mode="after")
    def assign_id(self) -> "BootstrapKnowledgeNodeCandidate":
        identity = ":".join(
            (
                self.entity.id,
                _text_hash(self.definition),
                *sorted(self.supporting_evidence_ids),
            )
        )
        expected = str(uuid5(NAMESPACE_URL, f"magicforge:bootstrap-node:{identity}"))
        if self.id and self.id != expected:
            raise ValueError("bootstrap node ID does not match contents")
        object.__setattr__(self, "id", expected)
        return self


class BootstrapRelationshipCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    relationship: KnowledgeRelationship
    assertion: str = Field(min_length=1)
    source_entity_name: str = Field(min_length=1)
    target_entity_name: str = Field(min_length=1)
    domains: list[MagicDomain] = Field(min_length=1)
    ontology_paths: list[str] = Field(min_length=1)
    topic_tags: list[str] = Field(default_factory=list)
    knowledge_origin: KnowledgeOrigin
    supporting_evidence_ids: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    confidence: ConfidenceAssessment
    verification_status: Literal["unverified"] = "unverified"
    bootstrap_generated: Literal[True] = True
    human_verified: Literal[False] = False
    created_by: Literal["glm", "demo-seed"] = "glm"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("supporting_evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: list[str]) -> list[str]:
        for value in values:
            UUID(value)
        return list(dict.fromkeys(values))

    @field_validator("ontology_paths")
    @classmethod
    def normalize_paths(cls, values: list[str]) -> list[str]:
        return normalize_ontology_paths(values)

    @model_validator(mode="after")
    def assign_id(self) -> "BootstrapRelationshipCandidate":
        identity = ":".join(
            (
                self.relationship.id,
                _text_hash(self.assertion),
                *sorted(self.supporting_evidence_ids),
            )
        )
        expected = str(
            uuid5(NAMESPACE_URL, f"magicforge:bootstrap-relationship:{identity}")
        )
        if self.id and self.id != expected:
            raise ValueError("bootstrap relationship ID does not match contents")
        object.__setattr__(self, "id", expected)
        return self


class BootstrapQdrantProjection(BaseModel):
    """A non-production point carrying explicit unverified-state markers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["bootstrap-qdrant-0.2"] = (
        BOOTSTRAP_PROJECTION_SCHEMA_VERSION
    )
    knowledge_unit_id: str
    artifact_type: ArtifactType
    artifact_id: str
    artifact_version: int = Field(default=1, ge=1)
    knowledge_type: KnowledgeType
    text: str = Field(min_length=1)
    title: str = Field(min_length=1)
    domain: list[MagicDomain] = Field(min_length=1)
    ontology_paths: list[str] = Field(min_length=1)
    topic_tags: list[str] = Field(default_factory=list)
    knowledge_origin: KnowledgeOrigin
    evidence_level: EvidenceLevel
    evidence_class: EvidenceClass
    claim_roles: list[ClaimRole] = Field(min_length=1)
    magic_application: list[str] = Field(default_factory=list)
    population_context: list[str] = Field(default_factory=list)
    performance_context: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_label: ConfidenceLabel
    limitations: list[str] = Field(min_length=1)
    source_type: SourceType
    source_id: str
    citation_id: str
    source_candidate_id: str
    document_id: str
    source_locator: str = Field(min_length=1)
    page_number: int | None = Field(default=None, ge=1)
    source_year: int | None = Field(default=None, ge=1800, le=2200)
    evidence_card_id: str | None = None
    canonical_claim_id: str | None = None
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    contradiction_status: ContradictionStatus
    entity_ids: list[str] = Field(default_factory=list)
    entity_types: list[EntityType] = Field(default_factory=list)
    relationship_ids: list[str] = Field(default_factory=list)
    relation_types: list[str] = Field(default_factory=list)
    secret_exposure_level: SecretExposureLevel
    secret_exposure_rank: int = Field(ge=0, le=3)
    sensitive_information_level: SensitiveInformationLevel
    source_review_status: Literal[ReviewStatus.BOOTSTRAP_PENDING_HUMAN_REVIEW] = (
        ReviewStatus.BOOTSTRAP_PENDING_HUMAN_REVIEW
    )
    review_status: Literal[ReviewStatus.BOOTSTRAP] = ReviewStatus.BOOTSTRAP
    verification_status: Literal["unverified"] = "unverified"
    bootstrap_generated: Literal[True] = True
    human_verified: Literal[False] = False
    approved: Literal[False] = False
    claim_eligibility: Literal[False] = False
    storage_permission: Literal[False] = False
    content_version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator(
        "knowledge_unit_id",
        "artifact_id",
        "source_id",
        "citation_id",
        "source_candidate_id",
        "document_id",
        "evidence_card_id",
        "canonical_claim_id",
    )
    @classmethod
    def validate_uuid_fields(cls, value: str | None) -> str | None:
        if value is not None:
            UUID(value)
        return value

    @field_validator(
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "entity_ids",
        "relationship_ids",
    )
    @classmethod
    def validate_uuid_lists(cls, values: list[str]) -> list[str]:
        for value in values:
            UUID(value)
        return list(dict.fromkeys(values))

    @field_validator("ontology_paths")
    @classmethod
    def normalize_paths(cls, values: list[str]) -> list[str]:
        return normalize_ontology_paths(values)

    @model_validator(mode="after")
    def enforce_bootstrap_contract(self) -> "BootstrapQdrantProjection":
        if self.knowledge_unit_id != self.id_for_artifact(
            self.artifact_id, self.artifact_version
        ):
            raise ValueError("bootstrap knowledge_unit_id is not deterministic")
        if self.secret_exposure_rank != self.secret_exposure_level.rank:
            raise ValueError("secret exposure rank does not match level")
        if not self.text.startswith("Knowledge type:"):
            raise ValueError("bootstrap projection must use structured derived text")
        lowered = self.text.casefold()
        if "<source_chunk>" in lowered or "source_chunk:" in lowered:
            raise ValueError("raw source chunks are prohibited in bootstrap projections")
        if self.artifact_type == ArtifactType.EVIDENCE_CARD:
            if not self.evidence_card_id or not self.canonical_claim_id:
                raise ValueError("evidence projection requires evidence identifiers")
        elif not self.supporting_evidence_ids:
            raise ValueError("knowledge projection requires supporting Evidence Cards")
        return self

    @staticmethod
    def id_for_artifact(artifact_id: str, artifact_version: int) -> str:
        UUID(artifact_id)
        return str(
            uuid5(
                NAMESPACE_URL,
                f"magicforge:bootstrap-projection:{artifact_id}:{artifact_version}:{BOOTSTRAP_PROJECTION_SCHEMA_VERSION}",
            )
        )

    @property
    def payload_checksum(self) -> str:
        return _json_hash(self.model_dump(mode="json"))

    def to_payload(self, *, manifest_id: str, manifest_hash: str) -> dict[str, object]:
        UUID(manifest_id)
        payload = self.model_dump(mode="json")
        payload["storage_manifest_id"] = manifest_id
        payload["manifest_hash"] = manifest_hash
        payload["payload_checksum"] = self.payload_checksum
        return payload


class BootstrapStorageManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    schema_version: Literal["bootstrap-manifest-0.2"] = (
        BOOTSTRAP_MANIFEST_SCHEMA_VERSION
    )
    manifest_hash: str = ""
    collection_name: Literal["magicforge_bootstrap_v02"] = BOOTSTRAP_COLLECTION_NAME
    projections: list[BootstrapQdrantProjection] = Field(min_length=1)
    expected_point_ids: list[str] = Field(default_factory=list)
    expected_point_count: int = Field(default=0, ge=0)
    bootstrap_generated: Literal[True] = True
    human_authorized: Literal[False] = False
    created_by: Literal["bootstrap-pipeline"] = "bootstrap-pipeline"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def assign_manifest_identity(self) -> "BootstrapStorageManifest":
        point_ids = [item.knowledge_unit_id for item in self.projections]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("bootstrap manifest contains duplicate point IDs")
        if self.expected_point_ids and self.expected_point_ids != point_ids:
            raise ValueError("bootstrap expected IDs do not match projections")
        if self.expected_point_count and self.expected_point_count != len(point_ids):
            raise ValueError("bootstrap expected count does not match projections")
        object.__setattr__(self, "expected_point_ids", point_ids)
        object.__setattr__(self, "expected_point_count", len(point_ids))
        digest = _json_hash(
            {
                "schema_version": self.schema_version,
                "collection_name": self.collection_name,
                "projections": sorted(
                    (item.knowledge_unit_id, item.payload_checksum)
                    for item in self.projections
                ),
            }
        )
        if self.manifest_hash and self.manifest_hash != digest:
            raise ValueError("bootstrap manifest hash does not match contents")
        object.__setattr__(self, "manifest_hash", digest)
        expected_id = str(
            uuid5(NAMESPACE_URL, f"magicforge:bootstrap-manifest:{digest}")
        )
        if self.id and self.id != expected_id:
            raise ValueError("bootstrap manifest ID does not match contents")
        object.__setattr__(self, "id", expected_id)
        return self


class BootstrapIngestionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    manifest_id: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_name: Literal["magicforge_bootstrap_v02"] = BOOTSTRAP_COLLECTION_NAME
    point_ids: list[str] = Field(min_length=1)
    actor: Literal["bootstrap-pipeline"] = "bootstrap-pipeline"
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    bootstrap_generated: Literal[True] = True

    @model_validator(mode="after")
    def assign_id(self) -> "BootstrapIngestionReceipt":
        UUID(self.manifest_id)
        for point_id in self.point_ids:
            UUID(point_id)
        expected = str(
            uuid5(
                NAMESPACE_URL,
                f"magicforge:bootstrap-receipt:{self.manifest_id}:{self.manifest_hash}",
            )
        )
        if self.id and self.id != expected:
            raise ValueError("bootstrap receipt ID does not match manifest")
        object.__setattr__(self, "id", expected)
        return self


class BootstrapProjectionBuilder:
    def from_evidence_card(self, card: EvidenceCard) -> BootstrapQdrantProjection:
        if card.review.review_status != ReviewStatus.BOOTSTRAP_GENERATED:
            raise ValueError("bootstrap projection requires bootstrap-generated card")
        if card.review.approved or card.confidence is None:
            raise ValueError("bootstrap cards must be unapproved and confidence-labelled")
        entity_ids = [
            card.source.source_id,
            *([card.source.research_paper_id] if card.source.research_paper_id else []),
            *card.mechanism_ids,
            *card.principle_ids,
        ]
        entity_types = [
            EntityType.SOURCE,
            *(
                [EntityType.RESEARCH_PAPER]
                if card.source.research_paper_id
                else []
            ),
            *([EntityType.COGNITIVE_MECHANISM] * len(card.mechanism_ids)),
            *([EntityType.PSYCHOLOGY_PRINCIPLE] * len(card.principle_ids)),
        ]
        text = "\n".join(
            (
                "Knowledge type: evidence",
                f"Claim: {card.claim}",
                f"Claim role: {card.claim_role.value}",
                f"Knowledge origin: {card.knowledge_origin.value}",
                f"Evidence class: {card.evidence_class.value}",
                f"Evidence locator: {_projection_locator(card.locator)}",
                f"Magic application: {card.magic_application or 'Not specified'}",
                f"Population context: {card.population_context or 'Not specified'}",
                f"Performance context: {card.performance_context or 'Not specified'}",
                f"Human verification: false",
                f"Limitations: {'; '.join(card.limitations)}",
            )
        )
        return self._base(
            card=card,
            artifact_type=ArtifactType.EVIDENCE_CARD,
            artifact_id=card.id,
            knowledge_type=KnowledgeType.EVIDENCE,
            text=text,
            title=card.claim[:160],
            domains=card.applicable_domain,
            ontology_paths=card.ontology_paths,
            topic_tags=card.topic_tags,
            knowledge_origin=card.knowledge_origin,
            confidence=card.confidence,
            limitations=card.limitations,
            evidence_card_id=card.id,
            canonical_claim_id=card.canonical_claim_id,
            supporting_evidence_ids=[card.id],
            entity_ids=entity_ids,
            entity_types=entity_types,
        )

    def from_node(
        self,
        node: BootstrapKnowledgeNodeCandidate,
        cards_by_id: dict[str, EvidenceCard],
    ) -> BootstrapQdrantProjection:
        cards = _supporting_cards(node.supporting_evidence_ids, cards_by_id)
        card = cards[0]
        text = "\n".join(
            (
                f"Knowledge type: {_knowledge_type_for_entity(node.entity.type).value}",
                f"Title: {node.entity.name}",
                f"Definition: {node.definition}",
                f"Knowledge origin: {node.knowledge_origin.value}",
                *_context_lines(cards),
                "Human verification: false",
                f"Limitations: {'; '.join(node.limitations)}",
            )
        )
        return self._base(
            card=card,
            artifact_type=ArtifactType.KNOWLEDGE_NODE,
            artifact_id=node.id,
            knowledge_type=_knowledge_type_for_entity(node.entity.type),
            text=text,
            title=node.entity.name,
            domains=node.domains,
            ontology_paths=node.ontology_paths,
            topic_tags=node.topic_tags,
            knowledge_origin=node.knowledge_origin,
            confidence=node.confidence,
            limitations=node.limitations,
            supporting_evidence_ids=node.supporting_evidence_ids,
            contradicting_evidence_ids=node.contradicting_evidence_ids,
            entity_ids=[node.entity.id],
            entity_types=[node.entity.type],
            context_cards=cards,
        )

    def from_relationship(
        self,
        item: BootstrapRelationshipCandidate,
        cards_by_id: dict[str, EvidenceCard],
    ) -> BootstrapQdrantProjection:
        cards = _supporting_cards(item.supporting_evidence_ids, cards_by_id)
        card = cards[0]
        text = "\n".join(
            (
                "Knowledge type: performance",
                f"Relationship: {item.source_entity_name} {item.relationship.type.value} {item.target_entity_name}",
                f"Assertion: {item.assertion}",
                f"Knowledge origin: {item.knowledge_origin.value}",
                *_context_lines(cards),
                "Human verification: false",
                f"Limitations: {'; '.join(item.limitations)}",
            )
        )
        return self._base(
            card=card,
            artifact_type=ArtifactType.RELATIONSHIP,
            artifact_id=item.id,
            knowledge_type=KnowledgeType.PERFORMANCE,
            text=text,
            title=(
                f"{item.source_entity_name} {item.relationship.type.value} "
                f"{item.target_entity_name}"
            ),
            domains=item.domains,
            ontology_paths=item.ontology_paths,
            topic_tags=item.topic_tags,
            knowledge_origin=item.knowledge_origin,
            confidence=item.confidence,
            limitations=item.limitations,
            supporting_evidence_ids=item.supporting_evidence_ids,
            entity_ids=[item.relationship.source_id, item.relationship.target_id],
            relationship_ids=[item.relationship.id],
            relation_types=[item.relationship.type.value],
            context_cards=cards,
        )

    def _base(
        self,
        *,
        card: EvidenceCard,
        artifact_type: ArtifactType,
        artifact_id: str,
        knowledge_type: KnowledgeType,
        text: str,
        title: str,
        domains: list[MagicDomain],
        ontology_paths: list[str],
        topic_tags: list[str],
        knowledge_origin: KnowledgeOrigin,
        confidence: ConfidenceAssessment,
        limitations: list[str],
        evidence_card_id: str | None = None,
        canonical_claim_id: str | None = None,
        supporting_evidence_ids: list[str] | None = None,
        contradicting_evidence_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        entity_types: list[EntityType] | None = None,
        relationship_ids: list[str] | None = None,
        relation_types: list[str] | None = None,
        context_cards: list[EvidenceCard] | None = None,
    ) -> BootstrapQdrantProjection:
        cards = context_cards or [card]
        return BootstrapQdrantProjection(
            knowledge_unit_id=BootstrapQdrantProjection.id_for_artifact(
                artifact_id, 1
            ),
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            knowledge_type=knowledge_type,
            text=text,
            title=title,
            domain=domains,
            ontology_paths=ontology_paths,
            topic_tags=topic_tags,
            knowledge_origin=knowledge_origin,
            evidence_level=card.evidence_level,
            evidence_class=card.evidence_class,
            claim_roles=_unique_values(item.claim_role for item in cards),
            magic_application=_unique_values(
                item.magic_application for item in cards if item.magic_application
            ),
            population_context=_unique_values(
                item.population_context for item in cards if item.population_context
            ),
            performance_context=_unique_values(
                item.performance_context for item in cards if item.performance_context
            ),
            confidence=confidence.score,
            confidence_label=confidence.label,
            limitations=limitations,
            source_type=card.source.source_type,
            source_id=card.source.source_id,
            citation_id=card.source.citation_id,
            source_candidate_id=card.source.source_candidate_id,
            document_id=card.source.document_id,
            source_locator=_projection_locator(card.locator),
            page_number=card.locator.page_number,
            source_year=card.source.source_year,
            evidence_card_id=evidence_card_id,
            canonical_claim_id=canonical_claim_id,
            supporting_evidence_ids=supporting_evidence_ids or [],
            contradicting_evidence_ids=contradicting_evidence_ids or [],
            contradiction_status=card.contradiction_status,
            entity_ids=entity_ids or [],
            entity_types=entity_types or [],
            relationship_ids=relationship_ids or [],
            relation_types=relation_types or [],
            secret_exposure_level=card.secret_exposure_level,
            secret_exposure_rank=card.secret_exposure_level.rank,
            sensitive_information_level=card.review.sensitive_information_level,
        )


def _supporting_cards(
    evidence_ids: list[str], cards_by_id: dict[str, EvidenceCard]
) -> list[EvidenceCard]:
    cards = [cards_by_id[value] for value in evidence_ids if value in cards_by_id]
    if not cards:
        raise ValueError("bootstrap knowledge requires a linked Evidence Card")
    return cards


def _context_lines(cards: list[EvidenceCard]) -> tuple[str, str, str, str, str]:
    roles = ", ".join(item.value for item in _unique_values(card.claim_role for card in cards))
    applications = "; ".join(
        _unique_values(card.magic_application for card in cards if card.magic_application)
    )
    populations = "; ".join(
        _unique_values(card.population_context for card in cards if card.population_context)
    )
    performances = "; ".join(
        _unique_values(card.performance_context for card in cards if card.performance_context)
    )
    return (
        f"Claim roles: {roles}",
        f"Evidence locator: {_projection_locator(cards[0].locator)}",
        f"Magic application: {applications or 'Not specified'}",
        f"Population context: {populations or 'Not specified'}",
        f"Performance context: {performances or 'Not specified'}",
    )


def _projection_locator(locator: EvidenceLocator) -> str:
    """Flatten structured locator detail without changing the projection schema."""

    parts = [locator.source_locator]
    if locator.section:
        parts.append(f"section={locator.section}")
    if locator.paragraph is not None:
        parts.append(f"paragraph={locator.paragraph}")
    if locator.printed_page:
        parts.append(f"printed_page={locator.printed_page}")
    if locator.figure_or_table:
        parts.append(f"figure_or_table={locator.figure_or_table}")
    if locator.timestamp_start is not None:
        end = (
            f"-{locator.timestamp_end:g}"
            if locator.timestamp_end is not None
            else ""
        )
        parts.append(f"timestamp={locator.timestamp_start:g}{end}")
    return " | ".join(parts)


def _unique_values(values) -> list:
    output = []
    seen = set()
    for value in values:
        key = str(getattr(value, "value", value)).casefold().strip()
        if key and key not in seen:
            seen.add(key)
            output.append(value)
    return output


def _knowledge_type_for_entity(entity_type: EntityType) -> KnowledgeType:
    mapping = {
        EntityType.EFFECT: KnowledgeType.EFFECT,
        EntityType.METHOD: KnowledgeType.METHOD,
        EntityType.TECHNIQUE: KnowledgeType.TECHNIQUE,
        EntityType.PSYCHOLOGY_PRINCIPLE: KnowledgeType.PSYCHOLOGY,
        EntityType.COGNITIVE_MECHANISM: KnowledgeType.PSYCHOLOGY,
        EntityType.PERFORMER: KnowledgeType.PERFORMANCE,
    }
    try:
        return mapping[entity_type]
    except KeyError as exc:
        raise ValueError(f"{entity_type.value} is provenance-only") from exc


def _text_hash(value: str) -> str:
    return hashlib.sha256(" ".join(value.split()).encode("utf-8")).hexdigest()


def _json_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BOOTSTRAP_COLLECTION_NAME",
    "BootstrapIngestionReceipt",
    "BootstrapKnowledgeNodeCandidate",
    "BootstrapProjectionBuilder",
    "BootstrapQdrantProjection",
    "BootstrapRelationshipCandidate",
    "BootstrapStorageManifest",
]
