"""Bootstrap source registration and unverified knowledge assembly."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from knowledge.bootstrap import (
    BootstrapKnowledgeNodeCandidate,
    BootstrapRelationshipCandidate,
)
from knowledge.evidence import EvidenceCard, KnowledgeOrigin, SourceType
from knowledge.governance import (
    ClaimEligibility,
    ContradictionCheckStatus,
    ExtractionPermission,
    MagicForgeMode,
    ReviewEvent,
    ReviewStatus,
    SensitiveInformationLevel,
    StoragePermission,
)
from knowledge.models import KnowledgeEntity, KnowledgeRelationship
from knowledge.ontology import canonical_entity_ontology_path
from research.citation.manager import CitationManager
from research.citation.models import CitationRecord
from research.extraction.models import KnowledgeProposal
from research.extraction.models import ExtractedEntity
from research.extraction.semantic import canonical_entity_key, validate_extracted_entity
from research.bootstrap.semantic import validate_relationship_entailment
from research.models import ContentAccess, ResearchCandidate, SourceCategory
from research.review.source_models import ExtractionScope, SourceApprovalRecord
from research.review.source_service import candidate_content_hash


class BootstrapModeError(ValueError):
    pass


class BootstrapSourceRegistrar:
    def __init__(self, mode: MagicForgeMode) -> None:
        if mode != MagicForgeMode.BOOTSTRAP:
            raise BootstrapModeError("bootstrap source registration is disabled")
        self.citations = CitationManager()

    def register(
        self,
        candidate: ResearchCandidate,
        citation: CitationRecord,
    ) -> SourceApprovalRecord:
        if candidate.content_access == ContentAccess.SEARCH_SNIPPET or not candidate.content:
            raise BootstrapModeError("bootstrap extraction requires acquired readable content")
        checked = self.citations.validate(citation)
        if checked.source_candidate_id != candidate.id:
            raise BootstrapModeError("citation does not refer to bootstrap candidate")
        source_type = _source_type(candidate)
        return SourceApprovalRecord(
            source_candidate_id=candidate.id,
            citation_id=checked.id,
            citation_status=checked.status,
            content_hash=candidate_content_hash(candidate),
            content_access=candidate.content_access,
            source_type=source_type,
            status=ReviewStatus.BOOTSTRAP_PENDING_HUMAN_REVIEW,
            claim_eligibility=ClaimEligibility.ELIGIBLE_WITH_LIMITS,
            extraction_permission=ExtractionPermission.SELECTED_SECTIONS,
            extraction_scope=ExtractionScope(
                locators=[candidate.url],
                notes="Bootstrap extraction is limited to the acquired MCP web extract.",
            ),
            storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
            sensitive_information_level=SensitiveInformationLevel.CONTROLLED,
            contradicting_evidence_checked=ContradictionCheckStatus.NOT_CHECKED,
            audit_log=[
                ReviewEvent(
                    action="bootstrap_source_registered",
                    actor="bootstrap-pipeline",
                    notes="Not human approved; exact content hash retained for later review.",
                )
            ],
        )


class BootstrapKnowledgeAssembler:
    def __init__(self) -> None:
        self.rejections: list[dict[str, str]] = []

    def assemble(
        self,
        proposal: KnowledgeProposal,
    ) -> tuple[
        list[BootstrapKnowledgeNodeCandidate],
        list[BootstrapRelationshipCandidate],
    ]:
        cards_by_id = {card.id: card for card in proposal.evidence_cards}
        nodes: list[BootstrapKnowledgeNodeCandidate] = []
        entity_by_id = {}
        for mapping in proposal.entity_proposals:
            cards = _cards(mapping.supporting_evidence_card_ids, cards_by_id)
            if not cards:
                continue
            entity_by_id[mapping.entity.id] = mapping.entity
            nodes.append(
                BootstrapKnowledgeNodeCandidate(
                    entity=mapping.entity,
                    definition=(
                        mapping.entity.description
                        or "; ".join(card.claim for card in cards[:3])
                    ),
                    domains=_unique_enum(
                        domain for card in cards for domain in card.applicable_domain
                    ),
                    ontology_paths=_unique(
                        path for card in cards for path in card.ontology_paths
                    ),
                    topic_tags=_unique(tag for card in cards for tag in card.topic_tags),
                    knowledge_origin=_origin(cards),
                    supporting_evidence_ids=[card.id for card in cards],
                    contradicting_evidence_ids=_unique(
                        value
                        for card in cards
                        for value in card.contradicting_evidence_ids
                    ),
                    limitations=_limitations(cards),
                    confidence=_confidence(cards),
                )
            )

        relationships: list[BootstrapRelationshipCandidate] = []
        for mapping in proposal.relationship_proposals:
            cards = _cards(mapping.supporting_evidence_card_ids, cards_by_id)
            source = entity_by_id.get(mapping.source_entity_id)
            target = entity_by_id.get(mapping.target_entity_id)
            if not cards or source is None or target is None:
                continue
            decision = validate_relationship_entailment(mapping, source, target, cards)
            if not decision.accepted:
                self.rejections.append(
                    {
                        "mapping_id": mapping.id,
                        "relation_type": mapping.type.value,
                        "source_entity": source.name,
                        "target_entity": target.name,
                        "reason": decision.reason,
                    }
                )
                continue
            relationship = KnowledgeRelationship(
                source_id=source.id,
                target_id=target.id,
                type=mapping.type,
                evidence=mapping.assertion,
                confidence=mapping.extraction_confidence,
                attributes={
                    "bootstrap_generated": True,
                    "human_verified": False,
                    "verification_status": "unverified",
                },
            )
            relationships.append(
                BootstrapRelationshipCandidate(
                    relationship=relationship,
                    assertion=mapping.assertion,
                    source_entity_name=source.name,
                    target_entity_name=target.name,
                    domains=_unique_enum(
                        domain for card in cards for domain in card.applicable_domain
                    ),
                    ontology_paths=_unique(
                        path for card in cards for path in card.ontology_paths
                    ),
                    topic_tags=_unique(tag for card in cards for tag in card.topic_tags),
                    knowledge_origin=_origin(cards),
                    supporting_evidence_ids=[card.id for card in cards],
                    limitations=_limitations(cards),
                    confidence=_confidence(cards),
                )
            )
        return nodes, relationships


def canonicalize_bootstrap_artifacts(
    nodes: list[BootstrapKnowledgeNodeCandidate],
    relationships: list[BootstrapRelationshipCandidate],
) -> tuple[list[BootstrapKnowledgeNodeCandidate], list[BootstrapRelationshipCandidate]]:
    """Merge duplicate entity proposals and remap equivalent relationship endpoints."""

    grouped_nodes: dict[str, list[BootstrapKnowledgeNodeCandidate]] = {}
    old_entity_to_key: dict[str, str] = {}
    for node in nodes:
        decision = validate_extracted_entity(
            ExtractedEntity(
                type=node.entity.type,
                name=node.entity.name,
                description=node.entity.description or node.definition,
                evidence_excerpt=node.definition,
                supporting_claims=[node.definition],
                confidence=node.confidence.claim_directness.score,
            )
        )
        if not decision.accepted:
            continue
        key = canonical_entity_key(node.entity.type, node.entity.name)
        grouped_nodes.setdefault(key, []).append(node)
        old_entity_to_key[node.entity.id] = key

    merged_nodes: list[BootstrapKnowledgeNodeCandidate] = []
    canonical_by_key = {}
    for key in sorted(grouped_nodes):
        group = grouped_nodes[key]
        first = group[0]
        source_ontology_paths = _unique(
            path for node in group for path in node.ontology_paths
        )
        aliases = _unique(
            [
                *first.entity.aliases,
                *(node.entity.name for node in group[1:]),
                *(alias for node in group[1:] for alias in node.entity.aliases),
            ]
        )
        entity = KnowledgeEntity(
            type=first.entity.type,
            name=first.entity.name,
            description=max(
                (node.entity.description for node in group if node.entity.description),
                key=len,
                default=None,
            ),
            aliases=[alias for alias in aliases if alias.casefold() != first.entity.name.casefold()],
            attributes={
                **first.entity.attributes,
                "canonical_registry_key": key,
                "merged_proposal_count": len(group),
                "source_ontology_paths": source_ontology_paths,
            },
        )
        merged = BootstrapKnowledgeNodeCandidate(
            entity=entity,
            definition=max((node.definition for node in group), key=len),
            domains=_unique_enum(domain for node in group for domain in node.domains),
            ontology_paths=[
                canonical_entity_ontology_path(
                    first.entity.type.value, first.entity.name
                )
            ],
            topic_tags=_unique(tag for node in group for tag in node.topic_tags),
            knowledge_origin=_merge_origins(node.knowledge_origin for node in group),
            supporting_evidence_ids=_unique(
                evidence_id
                for node in group
                for evidence_id in node.supporting_evidence_ids
            ),
            contradicting_evidence_ids=_unique(
                evidence_id
                for node in group
                for evidence_id in node.contradicting_evidence_ids
            ),
            limitations=_unique(
                limitation for node in group for limitation in node.limitations
            ),
            confidence=min((node.confidence for node in group), key=lambda item: item.score),
        )
        merged_nodes.append(merged)
        canonical_by_key[key] = merged.entity

    grouped_relationships: dict[
        tuple[str, str, str], list[BootstrapRelationshipCandidate]
    ] = {}
    for item in relationships:
        source_key = old_entity_to_key.get(item.relationship.source_id)
        target_key = old_entity_to_key.get(item.relationship.target_id)
        if source_key is None or target_key is None:
            continue
        key = (source_key, item.relationship.type.value, target_key)
        grouped_relationships.setdefault(key, []).append(item)

    merged_relationships: list[BootstrapRelationshipCandidate] = []
    for (source_key, _, target_key), group in sorted(grouped_relationships.items()):
        first = group[0]
        source = canonical_by_key[source_key]
        target = canonical_by_key[target_key]
        assertions = _unique(item.assertion for item in group)
        assertion = "; ".join(assertions)
        relationship = KnowledgeRelationship(
            source_id=source.id,
            target_id=target.id,
            type=first.relationship.type,
            evidence=assertion,
            confidence=min(
                value
                for value in (
                    item.relationship.confidence for item in group
                )
                if value is not None
            ),
            attributes={
                "bootstrap_generated": True,
                "human_verified": False,
                "verification_status": "unverified",
                "merged_proposal_count": len(group),
            },
        )
        merged_relationships.append(
            BootstrapRelationshipCandidate(
                relationship=relationship,
                assertion=assertion,
                source_entity_name=source.name,
                target_entity_name=target.name,
                domains=_unique_enum(domain for item in group for domain in item.domains),
                ontology_paths=[
                    f"magic.relationship.{first.relationship.type.value}"
                ],
                topic_tags=_unique(tag for item in group for tag in item.topic_tags),
                knowledge_origin=_merge_origins(item.knowledge_origin for item in group),
                supporting_evidence_ids=_unique(
                    evidence_id
                    for item in group
                    for evidence_id in item.supporting_evidence_ids
                ),
                limitations=_unique(
                    limitation for item in group for limitation in item.limitations
                ),
                confidence=min(
                    (item.confidence for item in group), key=lambda value: value.score
                ),
            )
        )
    return merged_nodes, merged_relationships


def source_review_item_id(source_approval: SourceApprovalRecord) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            f"magicforge:bootstrap-later-review:{source_approval.source_version_id}",
        )
    )


def _cards(ids: list[str], lookup: dict[str, EvidenceCard]) -> list[EvidenceCard]:
    return [lookup[value] for value in ids if value in lookup]


def _confidence(cards: list[EvidenceCard]):
    values = [card.confidence for card in cards if card.confidence is not None]
    if not values:
        raise BootstrapModeError("bootstrap knowledge requires confidence estimates")
    return min(values, key=lambda value: value.score)


def _origin(cards: list[EvidenceCard]) -> KnowledgeOrigin:
    origins = {card.knowledge_origin for card in cards}
    if len(origins) == 1:
        return next(iter(origins))
    return KnowledgeOrigin.PERSONAL_INTERPRETATION


def _merge_origins(values) -> KnowledgeOrigin:
    origins = set(values)
    if len(origins) == 1:
        return next(iter(origins))
    return KnowledgeOrigin.PERSONAL_INTERPRETATION


def _source_type(candidate: ResearchCandidate) -> SourceType:
    if candidate.source_category == SourceCategory.ACADEMIC:
        return SourceType.JOURNAL_ARTICLE
    text = f"{candidate.title} {candidate.url}".casefold()
    if "transcript" in text:
        return SourceType.TRANSCRIPT
    if any(term in text for term in ("interview", "conversation", "q&a", "podcast")):
        return SourceType.INTERVIEW
    return SourceType.WEB_ARTICLE


def _limitations(cards: list[EvidenceCard]) -> list[str]:
    return _unique(
        [
            *(value for card in cards for value in card.limitations),
            "Bootstrap-generated and not reviewed by a human.",
            "Contradicting evidence has not been systematically checked.",
        ]
    )


def _unique(values) -> list:
    output = []
    seen = set()
    for value in values:
        key = str(getattr(value, "value", value)).casefold().strip()
        if key and key not in seen:
            seen.add(key)
            output.append(value)
    return output


def _unique_enum(values) -> list:
    return _unique(values)


__all__ = [
    "BootstrapKnowledgeAssembler",
    "BootstrapModeError",
    "BootstrapSourceRegistrar",
    "canonicalize_bootstrap_artifacts",
    "source_review_item_id",
]
