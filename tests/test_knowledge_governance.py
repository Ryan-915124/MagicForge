from datetime import UTC, datetime
from uuid import uuid4

import pytest

from knowledge.ingestion import IngestionError, ingest_paths
from knowledge.models import Effect, EntityType
from knowledge.projections import ProjectionBuilder, StorageManifestService
from research.evidence import (
    ClaimRole,
    ConfidenceAssessment,
    ConfidenceDimension,
    ConfidenceLabel,
    ContradictionStatus,
    EvidenceCard,
    EvidenceClass,
    EvidenceLevel,
    EvidenceLocator,
    EvidenceReview,
    EvidenceSource,
    KnowledgeOrigin,
    MagicDomain,
    SourceType,
)
from research.extraction.models import EntityMappingProposal
from research.governance import (
    ClaimEligibility,
    ContradictionCheckStatus,
    ExtractionPermission,
    ReviewStatus,
    SensitiveInformationLevel,
    StoragePermission,
)
from research.review import (
    ClaimReviewService,
    InMemoryClaimReviewRepository,
    InMemoryMappingReviewRepository,
    MappingReviewService,
)


def _confidence(reviewer: str, score: float = 1.0) -> ConfidenceAssessment:
    def dimension(reason: str) -> ConfidenceDimension:
        return ConfidenceDimension(score=score, reason=reason)

    return ConfidenceAssessment(
        provenance_quality=dimension("Provenance checked."),
        method_rigor=dimension("Method checked."),
        claim_directness=dimension("Direct claim."),
        consistency=dimension("Consistency checked."),
        magic_applicability=dimension("Applicability checked."),
        assessed_by=reviewer,
    )


def _pending_card(
    claim: str,
    *,
    evidence_class: EvidenceClass = EvidenceClass.SYSTEMATIC_REVIEW,
) -> EvidenceCard:
    if evidence_class == EvidenceClass.EXPERT_INSTRUCTION:
        origin = KnowledgeOrigin.EXPERT_PRACTICE
        level = EvidenceLevel.PRACTITIONER
    else:
        origin = KnowledgeOrigin.SCIENTIFIC_EVIDENCE
        level = EvidenceLevel.REVIEW
    return EvidenceCard(
        claim=claim,
        claim_role=(
            ClaimRole.EXPERT_OPINION
            if evidence_class == EvidenceClass.EXPERT_INSTRUCTION
            else ClaimRole.RESULT
        ),
        applicable_domain=[MagicDomain.THEORY],
        ontology_paths=["psychology.attention"],
        knowledge_origin=origin,
        evidence_class=evidence_class,
        evidence_level=level,
        source=EvidenceSource(
            citation_id=str(uuid4()),
            source_id=str(uuid4()),
            source_candidate_id=str(uuid4()),
            source_version_id=str(uuid4()),
            document_id=str(uuid4()),
            source_type=SourceType.JOURNAL_ARTICLE,
            citation_status="full_text_verified",
            peer_review_status="peer_reviewed",
        ),
        locator=EvidenceLocator(media_type="web", source_locator="https://example.test/results"),
        evidence_excerpt="RAW SOURCE WORDING MUST NOT BE EMBEDDED",
        limitations=["Pending reviewer confirmation."],
        extraction_confidence=0.2,
        review=EvidenceReview(
            claim_eligibility=ClaimEligibility.ELIGIBLE,
            extraction_permission=ExtractionPermission.FULL_TEXT,
            storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
            sensitive_information_level=SensitiveInformationLevel.CONTROLLED,
        ),
        created_by="glm",
    )


def _approve_card(service: ClaimReviewService, card: EvidenceCard, reviewer: str = "Ryan Chen"):
    item = service.submit(card)
    return service.approve(
        item.id,
        reviewer=reviewer,
        reason="Claim, locator, class, limitations, and conflicts checked.",
        confidence=_confidence(reviewer),
        claim_eligibility=ClaimEligibility.ELIGIBLE,
        storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
        sensitive_information_level=SensitiveInformationLevel.CONTROLLED,
        contradiction_status=ContradictionStatus.NONE_FOUND,
        contradicting_evidence_checked=ContradictionCheckStatus.CHECKED_NONE_FOUND,
        limitations=["Applies only to the reviewed context."],
    )


def test_claims_from_one_source_are_reviewed_independently() -> None:
    service = ClaimReviewService(InMemoryClaimReviewRepository())
    first = service.submit(_pending_card("First atomic claim."))
    second = service.submit(_pending_card("Second atomic claim."))

    approved = service.approve(
        first.id,
        reviewer="Ryan Chen",
        reason="First claim verified.",
        confidence=_confidence("Ryan Chen"),
        claim_eligibility=ClaimEligibility.ELIGIBLE,
        storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
        sensitive_information_level=SensitiveInformationLevel.CONTROLLED,
        contradiction_status=ContradictionStatus.NONE_FOUND,
        contradicting_evidence_checked=ContradictionCheckStatus.CHECKED_NONE_FOUND,
        limitations=["Reviewed scope only."],
    )
    rejected = service.reject(
        second.id,
        reviewer="Ryan Chen",
        reason="The excerpt does not support this separate claim.",
    )

    assert approved.status == ReviewStatus.APPROVED
    assert rejected.status == ReviewStatus.REJECTED
    assert approved.card.id != rejected.card.id


def test_extraction_confidence_never_becomes_evidence_confidence() -> None:
    service = ClaimReviewService(InMemoryClaimReviewRepository())
    approved = _approve_card(service, _pending_card("A reviewed scientific claim."))

    assert approved.card.extraction_confidence == 0.2
    assert approved.card.confidence.score == 1.0
    assert approved.card.confidence.label == ConfidenceLabel.HIGH


def test_practitioner_evidence_confidence_is_capped() -> None:
    service = ClaimReviewService(InMemoryClaimReviewRepository())
    approved = _approve_card(
        service,
        _pending_card(
            "A practitioner recommends separating method and effect in time.",
            evidence_class=EvidenceClass.EXPERT_INSTRUCTION,
        ),
    )

    assert approved.card.knowledge_origin == KnowledgeOrigin.EXPERT_PRACTICE
    assert approved.card.confidence.label == ConfidenceLabel.MODERATE


def test_approved_mapping_builds_node_projection_without_raw_excerpt() -> None:
    claim_reviews = ClaimReviewService(InMemoryClaimReviewRepository())
    approved_card = _approve_card(
        claim_reviews,
        _pending_card("Attention may constrain what a spectator reports."),
    ).card
    proposal = EntityMappingProposal(
        entity=Effect(name="Perceived vanish"),
        evidence_excerpt=approved_card.evidence_excerpt,
        source_locator=approved_card.locator.source_locator,
        extraction_confidence=0.8,
        supporting_evidence_card_ids=[approved_card.id],
    )
    mapping_service = MappingReviewService(InMemoryMappingReviewRepository())
    item = mapping_service.submit_entity(proposal)
    approved_mapping = mapping_service.approve_entity(
        item.id,
        cards=[approved_card],
        reviewer="Ryan Chen",
        reason="Entity identity and definition checked.",
        definition="The audience experiences an object as no longer present.",
        domains=[MagicDomain.THEORY],
        ontology_paths=["effect.vanish"],
        limitations=["This definition does not identify a secret method."],
        confidence=_confidence("Ryan Chen"),
        knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
    )

    projection = ProjectionBuilder().from_knowledge_node(
        approved_mapping.approved_node,
        supporting_cards=[approved_card],
    )

    assert projection.entity_types == [EntityType.EFFECT]
    assert "RAW SOURCE WORDING" not in projection.text
    assert projection.supporting_evidence_ids == [approved_card.id]


def test_storage_authorization_requires_named_human_and_exact_manifest() -> None:
    service = ClaimReviewService(InMemoryClaimReviewRepository())
    card = _approve_card(service, _pending_card("An approved claim." )).card
    projection = ProjectionBuilder().from_evidence_card(card)
    manifests = StorageManifestService()
    pending = manifests.build(
        [projection],
        corpus_id="67d8671b-200b-4592-8784-13e6727514f9",
        validation_rule_versions={"projection_gate": "0.2"},
        collection_name="magicforge_knowledge_v01",
        created_by="research-pipeline",
    )

    with pytest.raises(ValueError, match="named human"):
        manifests.authorize(pending, authorizer="system", reason="Automatic")

    authorized = manifests.authorize(
        pending,
        authorizer="Ryan Chen",
        reason="Exact artifact versions and permissions checked.",
    )
    assert authorized.authorized is True
    assert authorized.expected_point_ids == [projection.knowledge_unit_id]


def test_legacy_ingest_paths_fails_closed() -> None:
    with pytest.raises(IngestionError, match="raw document ingestion is disabled"):
        ingest_paths([])
