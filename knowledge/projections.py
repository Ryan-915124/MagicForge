"""Approved knowledge artifacts and deterministic Qdrant retrieval projections."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from knowledge.models import EntityType, KnowledgeEntity, KnowledgeRelationship
from knowledge.evidence import (
    ClaimRole,
    ConfidenceAssessment,
    ConfidenceLabel,
    ContradictionStatus,
    EvidenceCard,
    EvidenceClass,
    EvidenceLevel,
    KnowledgeOrigin,
    MagicDomain,
    SourceType,
)
from knowledge.ontology import normalize_ontology_paths
from knowledge.governance import (
    ReviewStatus,
    SecretExposureLevel,
    SensitiveInformationLevel,
    require_human_identity,
)


PROJECTION_SCHEMA_VERSION = "qdrant-0.2"
KNOWLEDGE_ASSERTION_SCHEMA_VERSION = "knowledge-0.1"
MANIFEST_SCHEMA_VERSION = "manifest-0.2"


class KnowledgeType(StrEnum):
    EFFECT = "effect"
    METHOD = "method"
    TECHNIQUE = "technique"
    PSYCHOLOGY = "psychology"
    EVIDENCE = "evidence"
    PERFORMANCE = "performance"


class ArtifactType(StrEnum):
    EVIDENCE_CARD = "evidence_card"
    KNOWLEDGE_NODE = "knowledge_node"
    RELATIONSHIP = "relationship"


class ManifestStatus(StrEnum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    INGESTED = "ingested"
    FAILED = "failed"


class KnowledgeNodeVersion(BaseModel):
    """A reviewed assertion layer over a canonical entity identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    schema_version: str = KNOWLEDGE_ASSERTION_SCHEMA_VERSION
    entity: KnowledgeEntity
    version: int = Field(default=1, ge=1)
    definition: str = Field(min_length=1)
    domains: list[MagicDomain] = Field(min_length=1)
    ontology_paths: list[str] = Field(min_length=1)
    topic_tags: list[str] = Field(default_factory=list)
    knowledge_origin: KnowledgeOrigin
    supporting_evidence_ids: list[str] = Field(min_length=1)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(min_length=1)
    confidence: ConfidenceAssessment
    approved: Literal[True] = True
    review_item_id: str
    reviewer: str = Field(min_length=1)
    reviewed_at: datetime
    supersedes: list[str] = Field(default_factory=list)

    @field_validator(
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "supersedes",
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
    def assign_version_id(self) -> "KnowledgeNodeVersion":
        UUID(self.review_item_id)
        identity = ":".join(
            (
                self.entity.id,
                str(self.version),
                self.schema_version,
                _text_hash(self.definition),
                *sorted(self.supporting_evidence_ids),
            )
        )
        version_id = str(uuid5(NAMESPACE_URL, f"magicforge:node-version:{identity}"))
        if self.id and self.id != version_id:
            raise ValueError("knowledge node version ID does not match content")
        object.__setattr__(self, "id", version_id)
        return self


class KnowledgeRelationshipAssertion(BaseModel):
    """A versioned relationship whose assertion is backed by reviewed cards."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    schema_version: str = KNOWLEDGE_ASSERTION_SCHEMA_VERSION
    relationship: KnowledgeRelationship
    version: int = Field(default=1, ge=1)
    assertion: str = Field(min_length=1)
    knowledge_origin: KnowledgeOrigin
    supporting_evidence_ids: list[str] = Field(min_length=1)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(min_length=1)
    confidence: ConfidenceAssessment
    approved: Literal[True] = True
    review_item_id: str
    reviewer: str = Field(min_length=1)
    reviewed_at: datetime
    supersedes: list[str] = Field(default_factory=list)

    @field_validator(
        "supporting_evidence_ids",
        "contradicting_evidence_ids",
        "supersedes",
    )
    @classmethod
    def validate_uuid_lists(cls, values: list[str]) -> list[str]:
        for value in values:
            UUID(value)
        return list(dict.fromkeys(values))

    @model_validator(mode="after")
    def assign_version_id(self) -> "KnowledgeRelationshipAssertion":
        UUID(self.review_item_id)
        identity = ":".join(
            (
                self.relationship.id,
                str(self.version),
                self.schema_version,
                _text_hash(self.assertion),
                *sorted(self.supporting_evidence_ids),
            )
        )
        version_id = str(
            uuid5(NAMESPACE_URL, f"magicforge:relationship-version:{identity}")
        )
        if self.id and self.id != version_id:
            raise ValueError("relationship assertion ID does not match content")
        object.__setattr__(self, "id", version_id)
        return self


class QdrantProjection(BaseModel):
    """The only object that may cross the production vector-storage boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["qdrant-0.2"] = PROJECTION_SCHEMA_VERSION
    knowledge_unit_id: str
    artifact_type: ArtifactType
    artifact_id: str
    artifact_version: int = Field(ge=1)
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
    claim_eligibility: Literal[True] = True
    storage_permission: Literal[True] = True
    approved: Literal[True] = True
    review_status: Literal[ReviewStatus.INGESTED] = ReviewStatus.INGESTED
    review_item_id: str
    claim_review_item_ids: list[str] = Field(min_length=1)
    reviewed_at: datetime
    content_version: int = Field(default=1, ge=1)

    @field_validator(
        "knowledge_unit_id",
        "artifact_id",
        "source_id",
        "citation_id",
        "source_candidate_id",
        "document_id",
        "evidence_card_id",
        "canonical_claim_id",
        "review_item_id",
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
        "claim_review_item_ids",
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
    def enforce_storage_contract(self) -> "QdrantProjection":
        if self.knowledge_unit_id != self.id_for_artifact(
            self.artifact_id, self.artifact_version
        ):
            raise ValueError("knowledge_unit_id must be deterministic from artifact version")
        if self.secret_exposure_rank != self.secret_exposure_level.rank:
            raise ValueError("secret_exposure_rank does not match exposure level")
        if self.confidence_label == ConfidenceLabel.INSUFFICIENT:
            raise ValueError("insufficient evidence cannot enter a Qdrant projection")
        if self.contradiction_status == ContradictionStatus.NOT_CHECKED:
            raise ValueError("Qdrant projections require completed contradiction review")
        if self.artifact_type == ArtifactType.EVIDENCE_CARD:
            if not self.evidence_card_id or not self.canonical_claim_id:
                raise ValueError("evidence projections require card and canonical claim IDs")
        elif not self.supporting_evidence_ids:
            raise ValueError("knowledge projections require supporting Evidence Cards")
        if not self.text.startswith("Knowledge type:"):
            raise ValueError("projection text must use the reviewed structured template")
        if "<source_chunk>" in self.text.casefold() or "source_chunk:" in self.text.casefold():
            raise ValueError("temporary source chunks are prohibited in projection text")
        return self

    @staticmethod
    def id_for_artifact(artifact_id: str, artifact_version: int) -> str:
        UUID(artifact_id)
        return str(
            uuid5(
                NAMESPACE_URL,
                f"magicforge:projection:{artifact_id}:{artifact_version}:{PROJECTION_SCHEMA_VERSION}",
            )
        )

    @property
    def payload_checksum(self) -> str:
        return _json_hash(self.model_dump(mode="json"))

    def to_payload(
        self,
        *,
        corpus_id: str,
        manifest_id: str,
        manifest_hash: str,
    ) -> dict[str, object]:
        UUID(corpus_id)
        UUID(manifest_id)
        payload = self.model_dump(mode="json")
        payload["corpus_id"] = corpus_id
        payload["storage_manifest_id"] = manifest_id
        payload["manifest_hash"] = manifest_hash
        payload["payload_checksum"] = self.payload_checksum
        payload["bootstrap_generated"] = False
        payload["human_verified"] = True
        return payload


class StorageAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authorizer: str = Field(min_length=1)
    authorizer_user_id: str | None = None
    actor_role_snapshot: list[str] = Field(default_factory=list)
    authorized_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: str = Field(min_length=1)

    @field_validator("authorizer")
    @classmethod
    def require_human(cls, value: str) -> str:
        return require_human_identity(value)

    @field_validator("authorizer_user_id")
    @classmethod
    def validate_authorizer_id(cls, value: str | None) -> str | None:
        if value is not None:
            UUID(value)
        return value


class StorageManifest(BaseModel):
    """Immutable, content-addressed authorization bundle for Qdrant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    schema_version: str = MANIFEST_SCHEMA_VERSION
    manifest_hash: str = ""
    corpus_id: str
    projection_schema_version: str = PROJECTION_SCHEMA_VERSION
    validation_rule_versions: dict[str, str] = Field(default_factory=dict)
    authorized_sensitive_levels: list[SensitiveInformationLevel] = Field(
        default_factory=lambda: [
            SensitiveInformationLevel.PUBLIC,
            SensitiveInformationLevel.CONTROLLED,
        ]
    )
    collection_name: str = Field(min_length=1)
    projections: list[QdrantProjection] = Field(min_length=1)
    expected_point_ids: list[str] = Field(default_factory=list)
    expected_point_count: int = Field(default=0, ge=0)
    status: ManifestStatus = ManifestStatus.PENDING
    authorization: StorageAuthorization | None = None
    created_by: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def assign_manifest_identity(self) -> "StorageManifest":
        UUID(self.corpus_id)
        if self.projection_schema_version != PROJECTION_SCHEMA_VERSION:
            raise ValueError("manifest projection schema is unsupported")
        if not self.validation_rule_versions:
            raise ValueError("manifest requires deterministic validation rule versions")
        canonical_projections = sorted(
            self.projections,
            key=lambda projection: projection.knowledge_unit_id,
        )
        object.__setattr__(self, "projections", canonical_projections)
        point_ids = [projection.knowledge_unit_id for projection in canonical_projections]
        if len(point_ids) != len(set(point_ids)):
            raise ValueError("storage manifest contains duplicate point IDs")
        if self.expected_point_ids and self.expected_point_ids != point_ids:
            raise ValueError("expected_point_ids do not match projections")
        if self.expected_point_count and self.expected_point_count != len(point_ids):
            raise ValueError("expected_point_count does not match projections")
        object.__setattr__(self, "expected_point_ids", point_ids)
        object.__setattr__(self, "expected_point_count", len(point_ids))

        manifest_data = {
            "schema_version": self.schema_version,
            "corpus_id": self.corpus_id,
            "projection_schema_version": self.projection_schema_version,
            "validation_rule_versions": self.validation_rule_versions,
            "authorized_sensitive_levels": sorted(
                level.value for level in self.authorized_sensitive_levels
            ),
            "collection_name": self.collection_name,
            "projections": sorted(
                (
                    projection.knowledge_unit_id,
                    projection.payload_checksum,
                    projection.artifact_id,
                    projection.artifact_version,
                )
                for projection in self.projections
            ),
        }
        digest = _json_hash(manifest_data)
        if self.manifest_hash and self.manifest_hash != digest:
            raise ValueError("manifest_hash does not match immutable contents")
        object.__setattr__(self, "manifest_hash", digest)
        manifest_id = str(uuid5(NAMESPACE_URL, f"magicforge:manifest:{digest}"))
        if self.id and self.id != manifest_id:
            raise ValueError("manifest ID does not match immutable contents")
        object.__setattr__(self, "id", manifest_id)

        if self.status in {ManifestStatus.AUTHORIZED, ManifestStatus.INGESTED}:
            if self.authorization is None:
                raise ValueError("authorized/ingested manifests require authorization")
        elif self.authorization is not None:
            raise ValueError("pending/failed manifests cannot carry active authorization")
        return self

    @property
    def authorized(self) -> bool:
        return (
            self.status in {ManifestStatus.AUTHORIZED, ManifestStatus.INGESTED}
            and self.authorization is not None
        )


class IngestionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    manifest_id: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    corpus_id: str
    collection_name: str = Field(min_length=1)
    point_ids: list[str] = Field(min_length=1)
    payload_checksums: dict[str, str]
    actor: str = Field(min_length=1)
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    success: Literal[True] = True

    @field_validator("manifest_id", "corpus_id", "point_ids")
    @classmethod
    def validate_ids(cls, value):
        values = value if isinstance(value, list) else [value]
        for item in values:
            UUID(item)
        return value

    @model_validator(mode="after")
    def assign_receipt_id(self) -> "IngestionReceipt":
        if set(self.point_ids) != set(self.payload_checksums):
            raise ValueError("receipt checksums must cover every point exactly")
        receipt_id = str(
            uuid5(
                NAMESPACE_URL,
                f"magicforge:receipt:{self.corpus_id}:{self.manifest_id}:{self.manifest_hash}",
            )
        )
        if self.id and self.id != receipt_id:
            raise ValueError("receipt ID does not match manifest")
        object.__setattr__(self, "id", receipt_id)
        return self


class ProjectionBuilder:
    """Render approved structured knowledge; source prose is never embedded."""

    def from_evidence_card(self, card: EvidenceCard) -> QdrantProjection:
        if not card.projection_eligible:
            raise ValueError("Evidence Card is not approved and storage-eligible")
        assert card.confidence is not None
        assert card.review.review_item_id is not None
        assert card.review.review_date is not None
        text = _render_evidence_text(card)
        _ensure_no_review_excerpt(text, [card])
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
        return QdrantProjection(
            knowledge_unit_id=QdrantProjection.id_for_artifact(card.id, card.version),
            artifact_type=ArtifactType.EVIDENCE_CARD,
            artifact_id=card.id,
            artifact_version=card.version,
            knowledge_type=KnowledgeType.EVIDENCE,
            text=text,
            title=card.claim[:160],
            domain=card.applicable_domain,
            ontology_paths=card.ontology_paths,
            topic_tags=card.topic_tags,
            knowledge_origin=card.knowledge_origin,
            evidence_level=card.evidence_level,
            evidence_class=card.evidence_class,
            **_projection_context([card]),
            confidence=card.confidence.score,
            confidence_label=card.confidence.label,
            limitations=card.limitations,
            source_type=card.source.source_type,
            source_id=card.source.source_id,
            citation_id=card.source.citation_id,
            source_candidate_id=card.source.source_candidate_id,
            document_id=card.source.document_id,
            source_locator=card.locator.source_locator,
            page_number=card.locator.page_number,
            source_year=card.source.source_year,
            evidence_card_id=card.id,
            canonical_claim_id=card.canonical_claim_id,
            supporting_evidence_ids=[card.id],
            contradicting_evidence_ids=card.contradicting_evidence_ids,
            contradiction_status=card.contradiction_status,
            entity_ids=entity_ids,
            entity_types=entity_types,
            secret_exposure_level=card.secret_exposure_level,
            secret_exposure_rank=card.secret_exposure_level.rank,
            sensitive_information_level=card.review.sensitive_information_level,
            review_item_id=card.review.review_item_id,
            claim_review_item_ids=[card.review.review_item_id],
            reviewed_at=card.review.review_date,
            content_version=card.version,
        )

    def from_knowledge_node(
        self,
        node: KnowledgeNodeVersion,
        *,
        supporting_cards: list[EvidenceCard],
    ) -> QdrantProjection:
        cards = _require_supporting_cards(node.supporting_evidence_ids, supporting_cards)
        anchor = cards[0]
        assert anchor.confidence is not None
        text = _render_node_text(node, cards)
        _ensure_no_review_excerpt(text, cards)
        return QdrantProjection(
            knowledge_unit_id=QdrantProjection.id_for_artifact(node.id, node.version),
            artifact_type=ArtifactType.KNOWLEDGE_NODE,
            artifact_id=node.id,
            artifact_version=node.version,
            knowledge_type=_knowledge_type_for_entity(node.entity.type),
            text=text,
            title=node.entity.name,
            domain=node.domains,
            ontology_paths=node.ontology_paths,
            topic_tags=node.topic_tags,
            knowledge_origin=node.knowledge_origin,
            evidence_level=anchor.evidence_level,
            evidence_class=anchor.evidence_class,
            **_projection_context(cards),
            confidence=node.confidence.score,
            confidence_label=node.confidence.label,
            limitations=node.limitations,
            source_type=anchor.source.source_type,
            source_id=anchor.source.source_id,
            citation_id=anchor.source.citation_id,
            source_candidate_id=anchor.source.source_candidate_id,
            document_id=anchor.source.document_id,
            source_locator=anchor.locator.source_locator,
            page_number=anchor.locator.page_number,
            source_year=anchor.source.source_year,
            supporting_evidence_ids=node.supporting_evidence_ids,
            contradicting_evidence_ids=node.contradicting_evidence_ids,
            contradiction_status=_most_conservative_contradiction(cards),
            entity_ids=[node.entity.id],
            entity_types=[node.entity.type],
            secret_exposure_level=max(
                (card.secret_exposure_level for card in cards),
                key=lambda level: level.rank,
            ),
            secret_exposure_rank=max(
                card.secret_exposure_level.rank for card in cards
            ),
            sensitive_information_level=max(
                (card.review.sensitive_information_level for card in cards),
                key=_sensitivity_rank,
            ),
            review_item_id=node.review_item_id,
            claim_review_item_ids=[
                card.review.review_item_id
                for card in cards
                if card.review.review_item_id is not None
            ],
            reviewed_at=node.reviewed_at,
            content_version=node.version,
        )

    def from_relationship(
        self,
        assertion: KnowledgeRelationshipAssertion,
        *,
        source_entity: KnowledgeEntity,
        target_entity: KnowledgeEntity,
        supporting_cards: list[EvidenceCard],
        domains: list[MagicDomain],
        ontology_paths: list[str],
        topic_tags: list[str] | None = None,
    ) -> QdrantProjection:
        cards = _require_supporting_cards(
            assertion.supporting_evidence_ids,
            supporting_cards,
        )
        anchor = cards[0]
        if assertion.relationship.source_id != source_entity.id:
            raise ValueError("relationship source entity does not match assertion")
        if assertion.relationship.target_id != target_entity.id:
            raise ValueError("relationship target entity does not match assertion")
        text = "\n".join(
            (
                f"Knowledge type: {_knowledge_type_for_entity(source_entity.type).value}",
                f"Relationship: {source_entity.name} {assertion.relationship.type.value} {target_entity.name}",
                f"Assertion: {assertion.assertion}",
                f"Knowledge origin: {assertion.knowledge_origin.value}",
                *_context_lines(cards),
                f"Limitations: {'; '.join(assertion.limitations)}",
            )
        )
        _ensure_no_review_excerpt(text, cards)
        return QdrantProjection(
            knowledge_unit_id=QdrantProjection.id_for_artifact(
                assertion.id, assertion.version
            ),
            artifact_type=ArtifactType.RELATIONSHIP,
            artifact_id=assertion.id,
            artifact_version=assertion.version,
            knowledge_type=_knowledge_type_for_entity(source_entity.type),
            text=text,
            title=f"{source_entity.name} {assertion.relationship.type.value} {target_entity.name}",
            domain=domains,
            ontology_paths=ontology_paths,
            topic_tags=topic_tags or [],
            knowledge_origin=assertion.knowledge_origin,
            evidence_level=anchor.evidence_level,
            evidence_class=anchor.evidence_class,
            **_projection_context(cards),
            confidence=assertion.confidence.score,
            confidence_label=assertion.confidence.label,
            limitations=assertion.limitations,
            source_type=anchor.source.source_type,
            source_id=anchor.source.source_id,
            citation_id=anchor.source.citation_id,
            source_candidate_id=anchor.source.source_candidate_id,
            document_id=anchor.source.document_id,
            source_locator=anchor.locator.source_locator,
            page_number=anchor.locator.page_number,
            source_year=anchor.source.source_year,
            supporting_evidence_ids=assertion.supporting_evidence_ids,
            contradicting_evidence_ids=assertion.contradicting_evidence_ids,
            contradiction_status=_most_conservative_contradiction(cards),
            entity_ids=[source_entity.id, target_entity.id],
            entity_types=[source_entity.type, target_entity.type],
            relationship_ids=[assertion.relationship.id],
            relation_types=[assertion.relationship.type.value],
            secret_exposure_level=max(
                (card.secret_exposure_level for card in cards),
                key=lambda level: level.rank,
            ),
            secret_exposure_rank=max(
                card.secret_exposure_level.rank for card in cards
            ),
            sensitive_information_level=max(
                (card.review.sensitive_information_level for card in cards),
                key=_sensitivity_rank,
            ),
            review_item_id=assertion.review_item_id,
            claim_review_item_ids=[
                card.review.review_item_id
                for card in cards
                if card.review.review_item_id is not None
            ],
            reviewed_at=assertion.reviewed_at,
            content_version=assertion.version,
        )


class StorageManifestService:
    def build(
        self,
        projections: list[QdrantProjection],
        *,
        corpus_id: str,
        collection_name: str,
        created_by: str,
        validation_rule_versions: dict[str, str],
        authorized_sensitive_levels: list[SensitiveInformationLevel] | None = None,
    ) -> StorageManifest:
        return StorageManifest(
            corpus_id=corpus_id,
            collection_name=collection_name,
            projections=projections,
            created_by=created_by,
            validation_rule_versions=validation_rule_versions,
            authorized_sensitive_levels=(
                authorized_sensitive_levels
                or [
                    SensitiveInformationLevel.PUBLIC,
                    SensitiveInformationLevel.CONTROLLED,
                ]
            ),
        )

    def authorize(
        self,
        manifest: StorageManifest,
        *,
        authorizer: str,
        reason: str,
    ) -> StorageManifest:
        if manifest.status != ManifestStatus.PENDING:
            raise ValueError("only pending manifests may be authorized")
        authorization = StorageAuthorization(
            authorizer=authorizer,
            reason=reason.strip(),
        )
        return StorageManifest.model_validate(
            {
                **manifest.model_dump(mode="python"),
                "status": ManifestStatus.AUTHORIZED,
                "authorization": authorization,
            }
        )

    def mark_ingested(
        self,
        manifest: StorageManifest,
        receipt: IngestionReceipt,
    ) -> StorageManifest:
        if not manifest.authorized:
            raise ValueError("only authorized manifests may be marked ingested")
        if receipt.manifest_id != manifest.id or receipt.manifest_hash != manifest.manifest_hash:
            raise ValueError("receipt does not match manifest")
        if receipt.corpus_id != manifest.corpus_id:
            raise ValueError("receipt corpus does not match manifest")
        if receipt.point_ids != manifest.expected_point_ids:
            raise ValueError("receipt point IDs do not match manifest")
        return StorageManifest.model_validate(
            {
                **manifest.model_dump(mode="python"),
                "status": ManifestStatus.INGESTED,
            }
        )


def _render_evidence_text(card: EvidenceCard) -> str:
    parts = [
        "Knowledge type: evidence",
        f"Claim: {card.claim}",
        f"Claim role: {card.claim_role.value}",
        f"Knowledge origin: {card.knowledge_origin.value}",
        f"Evidence class: {card.evidence_class.value}",
        f"Magic application: {card.magic_application or 'Not specified'}",
        f"Population context: {card.population_context or 'Not specified'}",
        f"Performance context: {card.performance_context or 'Not specified'}",
    ]
    parts.append(f"Limitations: {'; '.join(card.limitations)}")
    return "\n".join(parts)


def _render_node_text(node: KnowledgeNodeVersion, cards: list[EvidenceCard]) -> str:
    return "\n".join(
        (
            f"Knowledge type: {_knowledge_type_for_entity(node.entity.type).value}",
            f"Title: {node.entity.name}",
            f"Definition: {node.definition}",
            f"Knowledge origin: {node.knowledge_origin.value}",
            *_context_lines(cards),
            f"Limitations: {'; '.join(node.limitations)}",
        )
    )


def _projection_context(cards: list[EvidenceCard]) -> dict[str, list]:
    return {
        "claim_roles": _unique_values(card.claim_role for card in cards),
        "magic_application": _unique_values(
            card.magic_application for card in cards if card.magic_application
        ),
        "population_context": _unique_values(
            card.population_context for card in cards if card.population_context
        ),
        "performance_context": _unique_values(
            card.performance_context for card in cards if card.performance_context
        ),
    }


def _context_lines(cards: list[EvidenceCard]) -> tuple[str, str, str, str]:
    context = _projection_context(cards)
    roles = ", ".join(role.value for role in context["claim_roles"])
    return (
        f"Claim roles: {roles}",
        f"Magic application: {'; '.join(context['magic_application']) or 'Not specified'}",
        f"Population context: {'; '.join(context['population_context']) or 'Not specified'}",
        f"Performance context: {'; '.join(context['performance_context']) or 'Not specified'}",
    )


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
        raise ValueError(f"{entity_type.value} is provenance-only and not a knowledge projection") from exc


def _require_supporting_cards(
    supporting_ids: list[str],
    cards: list[EvidenceCard],
) -> list[EvidenceCard]:
    by_id = {card.id: card for card in cards}
    if set(by_id) != set(supporting_ids):
        raise ValueError("projection requires the exact linked Evidence Cards")
    ordered = [by_id[card_id] for card_id in supporting_ids]
    if any(not card.projection_eligible for card in ordered):
        raise ValueError("all supporting Evidence Cards must be projection-eligible")
    return ordered


def _sensitivity_rank(level: SensitiveInformationLevel) -> int:
    return {
        SensitiveInformationLevel.PUBLIC: 0,
        SensitiveInformationLevel.CONTROLLED: 1,
        SensitiveInformationLevel.SECRET_METHOD: 2,
        SensitiveInformationLevel.RESTRICTED: 3,
    }[level]


def _most_conservative_contradiction(
    cards: list[EvidenceCard],
) -> ContradictionStatus:
    rank = {
        ContradictionStatus.NOT_CHECKED: 4,
        ContradictionStatus.UNRESOLVED: 3,
        ContradictionStatus.RESOLVED: 2,
        ContradictionStatus.NONE_FOUND: 1,
    }
    return max((card.contradiction_status for card in cards), key=rank.__getitem__)


def _ensure_no_review_excerpt(text: str, cards: list[EvidenceCard]) -> None:
    normalized_text = " ".join(text.casefold().split())
    for card in cards:
        excerpt = " ".join(card.evidence_excerpt.casefold().split())
        if len(excerpt) >= 32 and excerpt in normalized_text:
            raise ValueError(
                "projection text contains the Evidence Card review excerpt; "
                "store a reviewed derived statement instead"
            )


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
