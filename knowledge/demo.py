"""Deterministic, self-authored knowledge for the public Demo profile.

The committed JSON is a compact domain specification, not a captured research
run.  This module validates it with the existing MagicForge models and derives
stable Evidence Cards, nodes, relationships, and Qdrant projections entirely
offline.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from knowledge.bootstrap import (
    BOOTSTRAP_PROJECTION_SCHEMA_VERSION,
    BootstrapKnowledgeNodeCandidate,
    BootstrapProjectionBuilder,
    BootstrapQdrantProjection,
    BootstrapRelationshipCandidate,
)
from knowledge.evidence import (
    ApplicationOrigin,
    ClaimRole,
    ConfidenceAssessment,
    ConfidenceDimension,
    ContradictionStatus,
    EvidenceCard,
    EvidenceClass,
    EvidenceLevel,
    EvidenceLocator,
    EvidenceReview,
    EvidenceSource,
    KnowledgeOrigin,
    MagicDomain,
    MechanismStatus,
    SourceType,
)
from knowledge.governance import (
    ReviewStatus,
    SecretExposureLevel,
    SensitiveInformationLevel,
)
from knowledge.models import (
    EntityType,
    KnowledgeEntity,
    KnowledgeRelationship,
    RelationType,
)


DEMO_CORPUS_SCHEMA_VERSION = "demo-corpus-0.1"
DEMO_CORPUS_ID = "magicforge-demo-v01"
DEMO_COLLECTION_NAME = "magicforge_demo_v01"
DEMO_ACTOR = "demo-seed"


class DemoCorpusError(RuntimeError):
    """Raised when the committed Demo corpus loses its deterministic contract."""


class DemoSourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=1)
    author: str = Field(min_length=1)
    category: Literal["self_authored_demo"]
    source_type: Literal[SourceType.INTERNAL_ANALYSIS]
    synthetic: Literal[True]
    self_authored: Literal[True]
    redistribution_allowed: Literal[True]
    year: int = Field(ge=2026, le=2200)


class DemoClaimSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z0-9-]+$")
    source_key: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    excerpt: str = Field(min_length=1, max_length=1_000)
    locator: str = Field(min_length=1)
    claim_role: ClaimRole
    domains: list[MagicDomain] = Field(min_length=1)
    ontology_paths: list[str] = Field(min_length=1)
    topic_tags: list[str] = Field(default_factory=list)
    mechanism_node_keys: list[str] = Field(default_factory=list)
    principle_node_keys: list[str] = Field(default_factory=list)
    magic_application: str = Field(min_length=1)


class DemoNodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z0-9-]+$")
    entity_type: EntityType
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    domains: list[MagicDomain] = Field(min_length=1)
    ontology_paths: list[str] = Field(min_length=1)
    topic_tags: list[str] = Field(default_factory=list)
    supporting_claim_keys: list[str] = Field(min_length=1)


class DemoRelationshipSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[a-z0-9-]+$")
    source_node_key: str = Field(min_length=1)
    target_node_key: str = Field(min_length=1)
    relation_type: RelationType
    assertion: str = Field(min_length=1)
    domains: list[MagicDomain] = Field(min_length=1)
    ontology_paths: list[str] = Field(min_length=1)
    topic_tags: list[str] = Field(default_factory=list)
    supporting_claim_keys: list[str] = Field(min_length=1)


class DemoCorpusSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["demo-corpus-0.1"]
    corpus_id: Literal["magicforge-demo-v01"]
    collection_name: Literal["magicforge_demo_v01"]
    generated_at: datetime
    description: str = Field(min_length=1)
    synthetic: Literal[True]
    self_authored: Literal[True]
    redistribution_allowed: Literal[True]
    sources: list[DemoSourceSpec] = Field(min_length=5)
    claims: list[DemoClaimSpec] = Field(min_length=10)
    nodes: list[DemoNodeSpec] = Field(min_length=8)
    relationships: list[DemoRelationshipSpec] = Field(min_length=8)


class DemoSourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    candidate_id: str
    citation_id: str
    version_id: str
    document_id: str
    title: str
    author: str
    source_category: str
    source_type: SourceType
    source_year: int
    synthetic: Literal[True] = True
    self_authored: Literal[True] = True
    redistribution_allowed: Literal[True] = True


class DemoClaimRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    key: str
    source_id: str
    statement: str
    locator: str
    evidence_card_id: str


@dataclass(frozen=True, slots=True)
class DemoCorpusBundle:
    spec: DemoCorpusSpec
    sources: tuple[DemoSourceRecord, ...]
    claims: tuple[DemoClaimRecord, ...]
    evidence_cards: tuple[EvidenceCard, ...]
    nodes: tuple[BootstrapKnowledgeNodeCandidate, ...]
    relationships: tuple[BootstrapRelationshipCandidate, ...]
    projections: tuple[BootstrapQdrantProjection, ...]
    manifest_id: str
    manifest_hash: str
    receipt_id: str

    @property
    def expected_point_ids(self) -> tuple[str, ...]:
        return tuple(item.knowledge_unit_id for item in self.projections)

    @property
    def payload_checksums(self) -> dict[str, str]:
        return {
            item.knowledge_unit_id: item.payload_checksum
            for item in self.projections
        }

    def payloads(self) -> tuple[dict[str, object], ...]:
        return tuple(
            item.to_payload(
                manifest_id=self.manifest_id,
                manifest_hash=self.manifest_hash,
            )
            for item in self.projections
        )


def load_demo_bundle(path: str | Path) -> DemoCorpusBundle:
    """Load and deterministically materialize the committed Demo specification."""

    corpus_path = Path(path).resolve()
    try:
        raw = json.loads(corpus_path.read_text(encoding="utf-8"))
        spec = DemoCorpusSpec.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise DemoCorpusError("the Demo corpus specification is invalid") from exc
    return _build_bundle(spec)


def _build_bundle(spec: DemoCorpusSpec) -> DemoCorpusBundle:
    _require_unique("source", [item.key for item in spec.sources])
    _require_unique("claim", [item.key for item in spec.claims])
    _require_unique("node", [item.key for item in spec.nodes])
    _require_unique("relationship", [item.key for item in spec.relationships])
    if any(item.claim_role == ClaimRole.CONTEXT_ONLY for item in spec.claims):
        raise DemoCorpusError("a context-only Demo claim cannot create an Evidence Card")

    generated_at = spec.generated_at
    source_records = tuple(_source_record(item) for item in spec.sources)
    sources_by_key = dict(zip((item.key for item in spec.sources), source_records))
    entities_by_key = {
        item.key: KnowledgeEntity(
            type=item.entity_type,
            name=item.name,
            description=item.description,
            attributes={"demo_generated": True},
        )
        for item in spec.nodes
    }
    cards_by_claim_key: dict[str, EvidenceCard] = {}
    for claim in spec.claims:
        source = sources_by_key.get(claim.source_key)
        if source is None:
            raise DemoCorpusError("a Demo claim references an unknown source")
        mechanism_ids = _entity_ids(
            claim.mechanism_node_keys,
            entities_by_key,
            expected_type=EntityType.COGNITIVE_MECHANISM,
        )
        principle_ids = _entity_ids(
            claim.principle_node_keys,
            entities_by_key,
            expected_type=EntityType.PSYCHOLOGY_PRINCIPLE,
        )
        card = EvidenceCard(
            claim=claim.statement,
            claim_role=claim.claim_role,
            mechanism_ids=mechanism_ids,
            mechanism_status=(
                MechanismStatus.LINKED
                if mechanism_ids
                else MechanismStatus.UNRESOLVED
            ),
            principle_ids=principle_ids,
            applicable_domain=claim.domains,
            ontology_paths=claim.ontology_paths,
            topic_tags=claim.topic_tags,
            magic_application=claim.magic_application,
            application_origin=ApplicationOrigin.SOURCE_STATED,
            knowledge_origin=KnowledgeOrigin.PERSONAL_INTERPRETATION,
            evidence_class=EvidenceClass.ANALYST_INTERPRETATION,
            evidence_level=EvidenceLevel.ANECDOTAL,
            source=EvidenceSource(
                citation_id=source.citation_id,
                source_id=source.id,
                source_candidate_id=source.candidate_id,
                source_version_id=source.version_id,
                document_id=source.document_id,
                source_type=source.source_type,
                citation_status="self_authored_demo",
                peer_review_status="not_applicable",
                source_year=source.source_year,
            ),
            locator=EvidenceLocator(
                media_type="demo",
                source_locator=claim.locator,
            ),
            evidence_excerpt=claim.excerpt,
            limitations=[
                "Synthetic self-authored example; not scientific or practitioner evidence."
            ],
            population_context="Fictional demonstration audience.",
            performance_context="Offline public Demo profile only.",
            confidence=_demo_confidence(generated_at),
            extraction_confidence=1.0,
            contradiction_status=ContradictionStatus.NOT_CHECKED,
            review=EvidenceReview(
                review_status=ReviewStatus.BOOTSTRAP_GENERATED,
                sensitive_information_level=SensitiveInformationLevel.PUBLIC,
            ),
            secret_exposure_level=SecretExposureLevel.GENERAL_PRINCIPLE,
            created_at=generated_at,
            created_by=DEMO_ACTOR,
        )
        cards_by_claim_key[claim.key] = card

    nodes: list[BootstrapKnowledgeNodeCandidate] = []
    for item in spec.nodes:
        evidence_ids = _evidence_ids(item.supporting_claim_keys, cards_by_claim_key)
        nodes.append(
            BootstrapKnowledgeNodeCandidate(
                entity=entities_by_key[item.key],
                definition=item.definition,
                domains=item.domains,
                ontology_paths=item.ontology_paths,
                topic_tags=item.topic_tags,
                knowledge_origin=KnowledgeOrigin.PERSONAL_INTERPRETATION,
                supporting_evidence_ids=evidence_ids,
                limitations=[
                    "Synthetic demo concept; no empirical or practitioner authority is claimed."
                ],
                confidence=_demo_confidence(generated_at),
                created_by=DEMO_ACTOR,
                created_at=generated_at,
            )
        )

    relationships: list[BootstrapRelationshipCandidate] = []
    for item in spec.relationships:
        source = entities_by_key.get(item.source_node_key)
        target = entities_by_key.get(item.target_node_key)
        if source is None or target is None:
            raise DemoCorpusError("a Demo relationship references an unknown node")
        evidence_ids = _evidence_ids(item.supporting_claim_keys, cards_by_claim_key)
        relationship = KnowledgeRelationship(
            source_id=source.id,
            target_id=target.id,
            type=item.relation_type,
            evidence=item.assertion,
            confidence=0.5,
            attributes={"demo_generated": True},
        )
        relationships.append(
            BootstrapRelationshipCandidate(
                relationship=relationship,
                assertion=item.assertion,
                source_entity_name=source.name,
                target_entity_name=target.name,
                domains=item.domains,
                ontology_paths=item.ontology_paths,
                topic_tags=item.topic_tags,
                knowledge_origin=KnowledgeOrigin.PERSONAL_INTERPRETATION,
                supporting_evidence_ids=evidence_ids,
                limitations=[
                    "Synthetic relationship included only to demonstrate graph traversal."
                ],
                confidence=_demo_confidence(generated_at),
                created_by=DEMO_ACTOR,
                created_at=generated_at,
            )
        )

    cards = tuple(cards_by_claim_key[item.key] for item in spec.claims)
    cards_by_id = {item.id: item for item in cards}
    projection_builder = BootstrapProjectionBuilder()
    projections = tuple(
        _at_demo_time(item, generated_at)
        for item in (
            *(projection_builder.from_evidence_card(card) for card in cards),
            *(projection_builder.from_node(node, cards_by_id) for node in nodes),
            *(
                projection_builder.from_relationship(relationship, cards_by_id)
                for relationship in relationships
            ),
        )
    )
    _require_unique(
        "projection",
        [item.knowledge_unit_id for item in projections],
    )
    manifest_hash = _json_hash(
        {
            "schema_version": DEMO_CORPUS_SCHEMA_VERSION,
            "collection_name": DEMO_COLLECTION_NAME,
            "projections": sorted(
                (item.knowledge_unit_id, item.payload_checksum)
                for item in projections
            ),
        }
    )
    manifest_id = str(
        uuid5(NAMESPACE_URL, f"magicforge:demo-manifest:{manifest_hash}")
    )
    receipt_id = str(
        uuid5(
            NAMESPACE_URL,
            f"magicforge:demo-receipt:{manifest_id}:{manifest_hash}",
        )
    )
    claims = tuple(
        DemoClaimRecord(
            id=cards_by_claim_key[item.key].canonical_claim_id,
            key=item.key,
            source_id=sources_by_key[item.source_key].id,
            statement=item.statement,
            locator=item.locator,
            evidence_card_id=cards_by_claim_key[item.key].id,
        )
        for item in spec.claims
    )
    return DemoCorpusBundle(
        spec=spec,
        sources=source_records,
        claims=claims,
        evidence_cards=cards,
        nodes=tuple(nodes),
        relationships=tuple(relationships),
        projections=projections,
        manifest_id=manifest_id,
        manifest_hash=manifest_hash,
        receipt_id=receipt_id,
    )


def _source_record(item: DemoSourceSpec) -> DemoSourceRecord:
    source_id = _stable_id(f"source:{item.key}")
    return DemoSourceRecord(
        id=source_id,
        candidate_id=_stable_id(f"source-candidate:{item.key}"),
        citation_id=_stable_id(f"citation:{item.key}"),
        version_id=_stable_id(f"source-version:{item.key}:v1"),
        document_id=_stable_id(f"document:{item.key}"),
        title=item.title,
        author=item.author,
        source_category=item.category,
        source_type=SourceType(item.source_type),
        source_year=item.year,
    )


def _demo_confidence(assessed_at: datetime) -> ConfidenceAssessment:
    dimension = ConfidenceDimension(
        score=0.5,
        reason="Bounded confidence for a synthetic self-authored demonstration.",
    )
    return ConfidenceAssessment(
        provenance_quality=dimension,
        method_rigor=dimension,
        claim_directness=dimension,
        consistency=dimension,
        magic_applicability=dimension,
        assessed_by=DEMO_ACTOR,
        assessed_at=assessed_at,
    )


def _entity_ids(
    keys: list[str],
    entities: dict[str, KnowledgeEntity],
    *,
    expected_type: EntityType,
) -> list[str]:
    output: list[str] = []
    for key in keys:
        entity = entities.get(key)
        if entity is None or entity.type != expected_type:
            raise DemoCorpusError("a Demo claim contains an invalid entity link")
        output.append(entity.id)
    return output


def _evidence_ids(
    claim_keys: list[str],
    cards: dict[str, EvidenceCard],
) -> list[str]:
    try:
        return [cards[key].id for key in claim_keys]
    except KeyError as exc:
        raise DemoCorpusError("a Demo mapping references an unknown claim") from exc


def _at_demo_time(
    projection: BootstrapQdrantProjection,
    generated_at: datetime,
) -> BootstrapQdrantProjection:
    return projection.model_copy(update={"created_at": generated_at})


def _require_unique(label: str, values: list[str]) -> None:
    if len(values) != len(set(values)):
        raise DemoCorpusError(f"Demo corpus contains a duplicate {label} key")


def _stable_id(identity: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"magicforge:demo:{identity}"))


def _json_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DEMO_COLLECTION_NAME",
    "DEMO_CORPUS_ID",
    "DEMO_CORPUS_SCHEMA_VERSION",
    "DemoClaimRecord",
    "DemoCorpusBundle",
    "DemoCorpusError",
    "DemoSourceRecord",
    "load_demo_bundle",
]
