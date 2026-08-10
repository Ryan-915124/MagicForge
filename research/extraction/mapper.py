"""Map GLM proposals to pending Evidence Cards and reviewable mappings."""

from __future__ import annotations

from knowledge.chunking import RawChunk
from knowledge.models import (
    CognitiveMechanism,
    Effect,
    EntityType,
    KnowledgeEntity,
    Method,
    Performer,
    PsychologyPrinciple,
    ResearchPaper,
    Source,
    Technique,
)
from research.citation.models import CitationRecord
from knowledge.evidence import (
    ClaimRole,
    ConfidenceAssessment,
    ConfidenceDimension,
    EvidenceCard,
    EvidenceLocator,
    EvidenceReview,
    EvidenceSource,
    MechanismStatus,
    KnowledgeOrigin,
    classification_for_evidence_class,
    evidence_excerpt_is_locatable,
)
from research.extraction.semantic import canonical_entity_key, validate_extracted_entity
from research.extraction.quality import paragraph_for_excerpt
from research.extraction.models import (
    ChunkProposalArtifacts,
    EntityMappingProposal,
    RelationshipMappingProposal,
    ResearchExtractionResult,
)
from knowledge.governance import MagicForgeMode, ReviewStatus
from research.models import ContentAccess, ResearchCandidate, SourceCategory
from research.review.source_models import SourceApprovalRecord


_ENTITY_MODELS: dict[EntityType, type[KnowledgeEntity]] = {
    EntityType.EFFECT: Effect,
    EntityType.TECHNIQUE: Technique,
    EntityType.METHOD: Method,
    EntityType.PSYCHOLOGY_PRINCIPLE: PsychologyPrinciple,
    EntityType.PERFORMER: Performer,
    EntityType.COGNITIVE_MECHANISM: CognitiveMechanism,
}


def build_provenance_entities(
    candidate: ResearchCandidate,
    citation: CitationRecord,
) -> list[KnowledgeEntity]:
    source = Source(
        name=candidate.title,
        description=candidate.snippet or None,
        attributes={
            "url": candidate.url,
            "authors": candidate.authors,
            "year": candidate.published_year,
            "doi": candidate.doi,
            "citation_id": citation.id,
            "citation_status": citation.status.value,
            "source_category": candidate.source_category.value,
        },
    )
    entities: list[KnowledgeEntity] = [source]
    if candidate.source_category == SourceCategory.ACADEMIC:
        entities.append(
            ResearchPaper(
                name=candidate.title,
                attributes={
                    "doi": candidate.doi,
                    "authors": candidate.authors,
                    "year": candidate.published_year,
                    "venue": candidate.venue,
                    "citation_id": citation.id,
                },
            )
        )
    return entities


def map_research_chunk(
    raw_chunk: RawChunk,
    extracted: ResearchExtractionResult,
    candidate: ResearchCandidate,
    citation: CitationRecord,
    source_approval: SourceApprovalRecord,
    provenance_entities: list[KnowledgeEntity],
    *,
    mode: MagicForgeMode = MagicForgeMode.PRODUCTION,
) -> ChunkProposalArtifacts:
    """Create pending review artifacts; this function creates no storage DTO."""

    source = next(
        entity for entity in provenance_entities if entity.type == EntityType.SOURCE
    )
    paper = next(
        (
            entity
            for entity in provenance_entities
            if entity.type == EntityType.RESEARCH_PAPER
        ),
        None,
    )
    domain_entities: list[tuple[object, KnowledgeEntity]] = []
    lookup: dict[str, KnowledgeEntity] = {}
    canonical_entities: dict[str, KnowledgeEntity] = {}
    for item in extracted.entities:
        if not validate_extracted_entity(item).accepted:
            continue
        key = canonical_entity_key(item.type, item.name)
        if key in canonical_entities:
            lookup.setdefault(item.name.casefold(), canonical_entities[key])
            continue
        entity = _ENTITY_MODELS[item.type](
            name=item.name,
            description=item.description,
            attributes={"proposed_from_extraction": True},
        )
        canonical_entities[key] = entity
        domain_entities.append((item, entity))
        lookup.setdefault(entity.name.casefold(), entity)

    evidence_source = EvidenceSource(
        citation_id=citation.id,
        source_id=source.id,
        source_candidate_id=candidate.id,
        source_version_id=source_approval.source_version_id,
        document_id=candidate.id,
        source_type=source_approval.source_type,
        research_paper_id=paper.id if paper else None,
        citation_status=citation.status.value,
        peer_review_status=citation.peer_review_status.value,
        source_year=candidate.published_year,
    )
    locator = _locator(raw_chunk, candidate)
    cards: list[EvidenceCard] = []
    card_by_claim: dict[str, EvidenceCard] = {}
    for claim in extracted.claims:
        if claim.claim_role == ClaimRole.CONTEXT_ONLY:
            continue
        if not evidence_excerpt_is_locatable(claim.evidence_excerpt, raw_chunk.text):
            continue
        knowledge_origin, evidence_level = classification_for_evidence_class(
            claim.evidence_class
        )
        if (
            candidate.source_category == SourceCategory.PRACTITIONER
            and knowledge_origin == KnowledgeOrigin.SCIENTIFIC_EVIDENCE
        ):
            # Practitioner material may discuss science, but its own statements
            # cannot enter the scientific-evidence channel without a primary
            # academic source record and locator.
            continue
        mechanism_ids = _linked_ids(claim.mechanism_names, lookup, EntityType.COGNITIVE_MECHANISM)
        principle_ids = _linked_ids(
            claim.principle_names,
            lookup,
            EntityType.PSYCHOLOGY_PRINCIPLE,
        )
        card = EvidenceCard(
            claim=claim.statement,
            claim_role=claim.claim_role,
            claim_polarity=claim.claim_polarity,
            mechanism_ids=mechanism_ids,
            mechanism_status=(
                MechanismStatus.LINKED
                if mechanism_ids
                else MechanismStatus.UNRESOLVED
            ),
            principle_ids=principle_ids,
            applicable_domain=claim.applicable_domains,
            ontology_paths=claim.ontology_paths,
            topic_tags=claim.topic_tags,
            magic_application=claim.magic_application,
            application_origin=claim.application_origin,
            knowledge_origin=knowledge_origin,
            evidence_class=claim.evidence_class,
            evidence_level=evidence_level,
            source=evidence_source,
            locator=_locator(
                raw_chunk,
                candidate,
                evidence_excerpt=claim.evidence_excerpt,
            ).model_copy(
                update={"source_locator": claim.locator or locator.source_locator}
            ),
            evidence_excerpt=claim.evidence_excerpt,
            limitations=claim.limitations,
            population_context=claim.population_context,
            performance_context=claim.performance_context,
            extraction_confidence=claim.confidence,
            confidence=(
                _bootstrap_confidence(claim.confidence, bool(claim.magic_application))
                if mode == MagicForgeMode.BOOTSTRAP
                else None
            ),
            review=EvidenceReview(
                claim_eligibility=source_approval.claim_eligibility,
                extraction_permission=source_approval.extraction_permission,
                storage_permission=source_approval.storage_permission,
                approved=False,
                review_status=(
                    ReviewStatus.BOOTSTRAP_GENERATED
                    if mode == MagicForgeMode.BOOTSTRAP
                    else ReviewStatus.PENDING
                ),
                sensitive_information_level=(
                    source_approval.sensitive_information_level
                ),
            ),
            created_by="glm",
        )
        cards.append(card)
        card_by_claim[claim.statement.casefold()] = card

    entity_proposals = []
    for item, entity in domain_entities:
        supporting_ids = _supporting_card_ids(item.supporting_claims, card_by_claim)
        if not supporting_ids:
            continue
        entity_proposals.append(EntityMappingProposal(
            entity=entity,
            evidence_excerpt=item.evidence_excerpt,
            source_locator=locator.source_locator,
            extraction_confidence=item.confidence,
            supporting_evidence_card_ids=supporting_ids,
        ))
    relationship_proposals: list[RelationshipMappingProposal] = []
    for item in extracted.relationships:
        source_entity = lookup.get(item.source_name.casefold())
        target_entity = lookup.get(item.target_name.casefold())
        if source_entity is None or target_entity is None:
            continue
        supporting_ids = _supporting_card_ids(
            item.supporting_claims, card_by_claim
        )
        if not supporting_ids:
            continue
        relationship_proposals.append(
            RelationshipMappingProposal(
                source_entity_id=source_entity.id,
                target_entity_id=target_entity.id,
                type=item.type,
                assertion="; ".join(item.supporting_claims),
                evidence_excerpt=item.evidence_excerpt,
                source_locator=locator.source_locator,
                extraction_confidence=item.confidence,
                supporting_evidence_card_ids=supporting_ids,
            )
        )
    return ChunkProposalArtifacts(
        evidence_cards=cards,
        entity_proposals=entity_proposals,
        relationship_proposals=relationship_proposals,
    )


def _bootstrap_confidence(
    extraction_confidence: float,
    has_magic_application: bool,
) -> ConfidenceAssessment:
    directness = 1.0 if extraction_confidence >= 0.85 else 0.5 if extraction_confidence >= 0.55 else 0.0
    return ConfidenceAssessment(
        provenance_quality=ConfidenceDimension(
            score=0.5,
            reason="Metadata was programmatically checked; exact source interpretation is not human verified.",
        ),
        method_rigor=ConfidenceDimension(
            score=0.5,
            reason="Study or practitioner method classification is GLM-proposed in bootstrap mode.",
        ),
        claim_directness=ConfidenceDimension(
            score=directness,
            reason="Derived from GLM extraction confidence against the retained locator excerpt.",
        ),
        consistency=ConfidenceDimension(
            score=0.0,
            reason="Contradicting evidence has not yet been reviewed by a human.",
        ),
        magic_applicability=ConfidenceDimension(
            score=0.5 if has_magic_application else 0.0,
            reason=(
                "A source-linked magic application was proposed but is unverified."
                if has_magic_application
                else "No explicit magic application was retained."
            ),
        ),
        assessed_by="bootstrap-heuristic",
    )


def _locator(
    raw_chunk: RawChunk,
    candidate: ResearchCandidate,
    *,
    evidence_excerpt: str | None = None,
) -> EvidenceLocator:
    if candidate.content_access == ContentAccess.PDF_TEXT:
        media_type = "pdf"
    elif candidate.content_access == ContentAccess.WEB_EXTRACT:
        media_type = "web"
    else:
        media_type = "text"
    return EvidenceLocator(
        media_type=media_type,
        source_locator=raw_chunk.source_locator or candidate.url,
        page_number=raw_chunk.page_number,
        section=raw_chunk.heading,
        paragraph=(
            paragraph_for_excerpt(raw_chunk.text, evidence_excerpt)
            if evidence_excerpt and media_type != "pdf"
            else None
        ),
    )


def _linked_ids(
    names: list[str],
    lookup: dict[str, KnowledgeEntity],
    expected_type: EntityType,
) -> list[str]:
    ids = []
    for name in names:
        entity = lookup.get(name.casefold())
        if entity and entity.type == expected_type:
            ids.append(entity.id)
    return list(dict.fromkeys(ids))


def _supporting_card_ids(
    statements: list[str],
    card_by_claim: dict[str, EvidenceCard],
) -> list[str]:
    output = []
    for statement in statements:
        card = card_by_claim.get(" ".join(statement.split()).casefold())
        if card is not None:
            output.append(card.id)
    return list(dict.fromkeys(output))
