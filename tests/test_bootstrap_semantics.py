from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from knowledge.bootstrap import (
    BootstrapKnowledgeNodeCandidate,
    BootstrapProjectionBuilder,
)
from knowledge.evidence import (
    ApplicationOrigin,
    ClaimRole,
    ConfidenceAssessment,
    ConfidenceDimension,
    EvidenceCard,
    EvidenceClass,
    EvidenceLevel,
    EvidenceLocator,
    EvidenceReview,
    EvidenceSource,
    KnowledgeOrigin,
    MagicDomain,
    SourceType,
    evidence_excerpt_is_locatable,
)
from knowledge.governance import ReviewStatus
from knowledge.models import (
    CognitiveMechanism,
    Effect,
    EntityType,
    Method,
    Performer,
    PsychologyPrinciple,
    RelationType,
    Technique,
)
from knowledge.ontology import canonical_entity_ontology_path
from research.bootstrap.semantic import validate_relationship_entailment
from research.bootstrap.runner import (
    _filter_unsupported_entity_nodes,
    _remove_scientific_channel_leakage,
)
from research.bootstrap.service import canonicalize_bootstrap_artifacts
from research.extraction.models import (
    ExtractedClaim,
    ExtractedEntity,
    RelationshipMappingProposal,
)
from research.extraction.semantic import validate_extracted_entity
from research.extraction.quality import (
    normalize_bootstrap_claim_payload,
    normalize_bootstrap_evidence_card,
)


def _claim(**updates) -> ExtractedClaim:
    values = {
        "statement": "Participants may fail to notice an unexpected action.",
        "evidence_excerpt": "Participants may fail to notice an unexpected action.",
        "confidence": 0.9,
        "claim_role": ClaimRole.RESULT,
        "evidence_class": EvidenceClass.CONTROLLED_EXPERIMENT,
        "applicable_domains": [MagicDomain.THEORY],
        "ontology_paths": ["psychology.attention.inattentionalBlindness"],
        "limitations": ["Laboratory viewing conditions may limit generalization."],
    }
    values.update(updates)
    return ExtractedClaim(**values)


def _confidence() -> ConfidenceAssessment:
    dimension = ConfidenceDimension(score=0.5, reason="Bootstrap estimate only.")
    return ConfidenceAssessment(
        provenance_quality=dimension,
        method_rigor=dimension,
        claim_directness=dimension,
        consistency=ConfidenceDimension(
            score=0.0, reason="Contradictions have not been checked."
        ),
        magic_applicability=dimension,
        assessed_by="bootstrap-heuristic",
    )


def _card(
    claim: str = "Limited attention explains why spectators may miss an action.",
    excerpt: str | None = None,
    *,
    role: ClaimRole = ClaimRole.RESULT,
) -> EvidenceCard:
    excerpt = excerpt or claim
    return EvidenceCard(
        claim=claim,
        claim_role=role,
        applicable_domain=[MagicDomain.CLOSE_UP],
        ontology_paths=["psychology.attention.selectiveAttention"],
        magic_application="Place the secret action outside the selected focus.",
        application_origin=ApplicationOrigin.SOURCE_STATED,
        knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        evidence_class=EvidenceClass.SYSTEMATIC_REVIEW,
        evidence_level=EvidenceLevel.REVIEW,
        source=EvidenceSource(
            citation_id=str(uuid4()),
            source_id=str(uuid4()),
            source_candidate_id=str(uuid4()),
            source_version_id=str(uuid4()),
            document_id=str(uuid4()),
            source_type=SourceType.JOURNAL_ARTICLE,
            citation_status="metadata_verified",
            peer_review_status="peer_reviewed",
        ),
        locator=EvidenceLocator(
            media_type="web", source_locator="https://example.test/source#results"
        ),
        evidence_excerpt=excerpt,
        limitations=["The performance setting may change the effect."],
        population_context="Adult spectators in a controlled study.",
        performance_context="A close-up magic demonstration.",
        confidence=_confidence(),
        extraction_confidence=0.9,
        review=EvidenceReview(review_status=ReviewStatus.BOOTSTRAP_GENERATED),
        created_by="glm",
    )


def _extracted_entity(entity_type: EntityType, name: str, description: str) -> ExtractedEntity:
    return ExtractedEntity(
        type=entity_type,
        name=name,
        description=description,
        evidence_excerpt=description,
        supporting_claims=["A linked claim."],
        confidence=0.8,
    )


def _mapping(source, target, relation: RelationType, text: str, card: EvidenceCard):
    return RelationshipMappingProposal(
        source_entity_id=source.id,
        target_entity_id=target.id,
        type=relation,
        assertion=text,
        evidence_excerpt=text,
        source_locator=card.locator.source_locator,
        extraction_confidence=0.8,
        supporting_evidence_card_ids=[card.id],
    )


def test_claim_role_controls_evidence_class_and_context_only_is_not_evidence() -> None:
    with pytest.raises(ValidationError, match="background claims cannot use controlled_experiment"):
        _claim(claim_role=ClaimRole.BACKGROUND)
    with pytest.raises(ValidationError, match="hypothesis claims cannot use controlled_experiment"):
        _claim(claim_role=ClaimRole.HYPOTHESIS)
    with pytest.raises(ValidationError, match="removes uncertainty"):
        _claim(
            claim_role=ClaimRole.DISCUSSION,
            evidence_class=EvidenceClass.NARRATIVE_REVIEW,
            statement="Participants fail to notice an unexpected action.",
        )
    with pytest.raises(ValidationError, match="context_only claims cannot create"):
        _card(role=ClaimRole.CONTEXT_ONLY)


def test_ontology_paths_are_dot_only_and_normalized() -> None:
    claim = _claim()
    assert claim.ontology_paths == ["psychology.attention.inattentional_blindness"]
    with pytest.raises(ValidationError, match="slash paths are rejected"):
        _claim(ontology_paths=["psychology/attention/change_blindness"])
    assert canonical_entity_ontology_path("performer", "Dai Vernon") == (
        "magic.performer.dai_vernon"
    )


def test_evidence_excerpt_must_be_locatable_in_the_source_window() -> None:
    source = "Observers’ attention may fail during the unexpected action."
    assert evidence_excerpt_is_locatable(
        "Observers' attention may fail during the unexpected action.", source
    )
    assert not evidence_excerpt_is_locatable(
        "Observers always miss secret actions.", source
    )


@pytest.mark.parametrize(
    ("entity_type", "name", "description", "reason"),
    [
        (EntityType.COGNITIVE_MECHANISM, "Prefrontal cortex", "A brain region activated during trials.", "brain region"),
        (EntityType.METHOD, "Control condition", "The experimental control condition.", "experiment condition"),
        (EntityType.TECHNIQUE, "Magic technique", "A generic technique.", "generic noun"),
        (EntityType.TECHNIQUE, "Ambitious Card Routine", "A complete trick using card handling skill.", "routine title"),
        (EntityType.EFFECT, "Creativity enhancement", "A divergent thinking training outcome.", "training or intervention"),
        (EntityType.COGNITIVE_MECHANISM, "Pupil dilatation", "A physiological indicator of surprise.", "measure, outcome"),
        (EntityType.COGNITIVE_MECHANISM, "Psychological mechanisms", "General mechanisms discussed by the authors.", "generic noun"),
        (EntityType.METHOD, "MiniMagic kindergarten program", "A training program using magic.", "training program"),
        (EntityType.PERFORMER, "Sheep", "An experimental participant group.", "research roles"),
        (EntityType.PERFORMER, "Hungarian magician", "An unnamed magician in the intervention.", "unnamed magician"),
        (EntityType.TECHNIQUE, "Social cues", "Gaze and gesture stimuli presented to spectators.", "stimulus"),
        (EntityType.TECHNIQUE, "Crib Sheets", "Written memorization aids.", "routine title"),
        (EntityType.EFFECT, "French Drop sleight", "A sleight that apparently makes a coin vanish.", "not an audience effect"),
        (EntityType.EFFECT, "Floating Rose routine", "A routine in which a rose appears to float.", "training or intervention"),
        (EntityType.TECHNIQUE, "Physical techniques of misdirection", "Physical triggers for attention.", "routine title"),
        (EntityType.EFFECT, "Gaze cueing effect", "Participants responded faster to gazed-at targets.", "experimental outcome"),
        (EntityType.EFFECT, "Object change detection", "A measured detection outcome for changed objects.", "experimental outcome"),
        (EntityType.COGNITIVE_MECHANISM, "Information processing mechanism", "A generic process label.", "generic noun"),
        (EntityType.COGNITIVE_MECHANISM, "Anticipation and preparation", "A broad task period label.", "generic noun"),
        (EntityType.PSYCHOLOGY_PRINCIPLE, "Unified audience feeling", "Shared surprise may unite an audience.", "generic noun"),
    ],
)
def test_entity_validation_rejects_audit_failure_categories(
    entity_type, name, description, reason
) -> None:
    decision = validate_extracted_entity(
        _extracted_entity(entity_type, name, description)
    )
    assert decision.accepted is False
    assert reason in decision.reason


def test_canonical_registry_merges_duplicate_entities_before_projection() -> None:
    card_ids = [str(uuid4()), str(uuid4())]
    nodes = [
        BootstrapKnowledgeNodeCandidate(
            entity=Technique(name="False Transfer"),
            definition="An executable transfer that creates a false impression.",
            domains=[MagicDomain.CLOSE_UP],
            ontology_paths=["technique.transfer.falseTransfer"],
            knowledge_origin=KnowledgeOrigin.EXPERT_PRACTICE,
            supporting_evidence_ids=[card_ids[0]],
            limitations=["Unverified."],
            confidence=_confidence(),
        ),
        BootstrapKnowledgeNodeCandidate(
            entity=Technique(name="false-transfer"),
            definition="A transfer action in which the apparent destination differs from the actual one.",
            domains=[MagicDomain.CARD],
            ontology_paths=["technique.transfer.false_transfer"],
            knowledge_origin=KnowledgeOrigin.EXPERT_PRACTICE,
            supporting_evidence_ids=[card_ids[1]],
            limitations=["Context dependent."],
            confidence=_confidence(),
        ),
    ]
    merged, relationships = canonicalize_bootstrap_artifacts(nodes, [])
    assert len(merged) == 1
    assert relationships == []
    assert set(merged[0].supporting_evidence_ids) == set(card_ids)
    assert "false-transfer" in merged[0].entity.aliases


def test_canonical_registry_merges_equivoque_aliases_and_filters_cached_invalid_nodes() -> None:
    card_ids = [str(uuid4()), str(uuid4()), str(uuid4())]
    nodes = [
        BootstrapKnowledgeNodeCandidate(
            entity=Technique(name="Equivoque force"),
            definition="An executable verbal forcing skill.",
            domains=[MagicDomain.MENTALISM],
            ontology_paths=["magic.technique.forcing.equivoque"],
            knowledge_origin=KnowledgeOrigin.EXPERT_PRACTICE,
            supporting_evidence_ids=[card_ids[0]],
            limitations=["Unverified."],
            confidence=_confidence(),
        ),
        BootstrapKnowledgeNodeCandidate(
            entity=Technique(name="Equivoque forcing technique"),
            definition="A verbal force that constrains an apparent choice.",
            domains=[MagicDomain.MENTALISM],
            ontology_paths=["magic.technique.forcing.equivoque"],
            knowledge_origin=KnowledgeOrigin.EXPERT_PRACTICE,
            supporting_evidence_ids=[card_ids[1]],
            limitations=["Unverified."],
            confidence=_confidence(),
        ),
        BootstrapKnowledgeNodeCandidate(
            entity=Performer(name="Sheep"),
            definition="An experimental participant group.",
            domains=[MagicDomain.THEORY],
            ontology_paths=["psychology.belief_systems"],
            knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
            supporting_evidence_ids=[card_ids[2]],
            limitations=["Invalid cached proposal."],
            confidence=_confidence(),
        ),
    ]
    merged, _ = canonicalize_bootstrap_artifacts(nodes, [])
    assert len(merged) == 1
    assert set(merged[0].supporting_evidence_ids) == set(card_ids[:2])


def test_cached_practitioner_scientific_channel_leakage_is_removed() -> None:
    cards, nodes, relationships = _remove_scientific_channel_leakage(
        [_card()], [], []
    )
    assert cards == []
    assert nodes == []
    assert relationships == []


def test_relationship_gate_rejects_non_entailing_edges() -> None:
    card = _card()
    effect = Effect(name="Perceived vanish")
    performer = Performer(name="Jane Magician")
    mechanism = CognitiveMechanism(name="Attentional selection")
    method = Method(name="Secret switch")
    technique = Technique(name="False transfer")
    principle = PsychologyPrinciple(name="Selective attention")

    wrong_direction = _mapping(
        technique, performer, RelationType.PERFORMED_BY, "Jane performs the technique.", card
    )
    assert not validate_relationship_entailment(
        wrong_direction, technique, performer, [card]
    ).accepted

    correlation = _mapping(
        effect,
        mechanism,
        RelationType.REQUIRES,
        "The vanish correlated with activation of attentional selection.",
        card,
    )
    assert not validate_relationship_entailment(
        correlation, effect, mechanism, [card]
    ).accepted

    co_mention = _mapping(
        effect,
        technique,
        RelationType.USES,
        "The article mentions a perceived vanish and false transfer.",
        card,
    )
    assert not validate_relationship_entailment(
        co_mention, effect, technique, [card]
    ).accepted

    stimulus = _mapping(
        effect,
        method,
        RelationType.USES,
        "The experiment used a video condition as its stimulus.",
        card,
    )
    assert not validate_relationship_entailment(
        stimulus, effect, method, [card]
    ).accepted

    explanatory = _mapping(
        mechanism,
        principle,
        RelationType.EXPLAINS,
        "Attentional selection explains selective attention during the effect.",
        card,
    )
    assert validate_relationship_entailment(
        explanatory, mechanism, principle, [card]
    ).accepted


def test_bootstrap_projection_preserves_application_and_context() -> None:
    card_payload = _card().model_dump(mode="json")
    card_payload["locator"]["paragraph"] = 3
    card = EvidenceCard.model_validate(card_payload)
    projection = BootstrapProjectionBuilder().from_evidence_card(card)
    assert "Magic application:" in projection.text
    assert "Population context:" in projection.text
    assert "Performance context:" in projection.text
    assert projection.magic_application == [card.magic_application]
    assert projection.population_context == [card.population_context]
    assert projection.performance_context == [card.performance_context]
    assert projection.claim_roles == [ClaimRole.RESULT]
    assert projection.source_locator.endswith("paragraph=3")
    assert "Evidence locator:" in projection.text


def test_bootstrap_quality_downgrades_observational_claim_design() -> None:
    payload, reasons = normalize_bootstrap_claim_payload(
        {
            "statement": "Convergent thinking predicts false recognition.",
            "evidence_excerpt": (
                "Convergent thinking accounted for 34.6% of the variance in "
                "false recognition."
            ),
            "limitations": ["Correlational relationship."],
            "claim_role": "result",
            "evidence_class": "controlled_experiment",
            "magic_application": None,
            "application_origin": "not_applicable",
        }
    )
    assert payload["evidence_class"] == "observational_study"
    assert "evidence_class_downgraded_to_observational" in reasons


def test_bootstrap_quality_does_not_treat_learned_associations_as_observational() -> None:
    payload, reasons = normalize_bootstrap_claim_payload(
        {
            "statement": "Experiment 4 tested associative inference trials.",
            "evidence_excerpt": (
                "Participants were tested on associative inference trials, "
                "eliminating retrieval of directly learned associations."
            ),
            "limitations": ["Procedural detail only."],
            "claim_role": "method",
            "evidence_class": "observational_study",
            "magic_application": None,
            "application_origin": "not_applicable",
        }
    )
    assert payload["evidence_class"] == "controlled_experiment"
    assert reasons == ("experimental_method_class_restored",)


def test_bootstrap_quality_downgrades_existing_deliberate_practice_groups() -> None:
    payload, reasons = normalize_bootstrap_claim_payload(
        {
            "statement": "The present study reported a smaller effect size.",
            "evidence_excerpt": "The size of the effect was considerably smaller.",
            "limitations": ["Result described in the abstract."],
            "claim_role": "result",
            "evidence_class": "quasi_experiment",
            "magic_application": None,
            "application_origin": "not_applicable",
        },
        source_title="The role of deliberate practice in expert performance",
    )
    assert payload["evidence_class"] == "observational_study"
    assert reasons == ("evidence_class_downgraded_to_observational",)


def test_bootstrap_quality_removes_application_enum_and_adds_paragraph() -> None:
    card = _card()
    payload = card.model_dump(mode="json")
    payload["magic_application"] = "source_stated"
    placeholder = EvidenceCard.model_validate(payload)
    source_text = (
        "Opening context.\n\n"
        "Limited attention explains why spectators may miss an action.\n\n"
        "Closing context."
    )

    correction = normalize_bootstrap_evidence_card(
        placeholder,
        selected_source_text=source_text,
    )

    assert correction.card.id == card.id
    assert correction.card.magic_application is None
    assert correction.card.application_origin == ApplicationOrigin.NOT_APPLICABLE
    assert correction.card.locator.paragraph == 2
    assert correction.card.confidence is not None
    assert correction.card.confidence.magic_applicability.score == 0.0
    assert set(correction.reasons) == {
        "magic_application_placeholder_removed",
        "paragraph_locator_added",
    }


def test_bootstrap_node_requires_explicit_support_for_named_construct() -> None:
    card = _card(
        claim="The study formed composite measures of both constructs.",
        excerpt="We formed composite measures of both cognitive constructs.",
    )
    node = BootstrapKnowledgeNodeCandidate(
        entity=CognitiveMechanism(name="Working memory"),
        definition="A temporary information-processing system.",
        domains=[MagicDomain.THEORY],
        ontology_paths=["psychology.cognitive_mechanism.working_memory"],
        knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        supporting_evidence_ids=[card.id],
        limitations=["Unverified."],
        confidence=_confidence(),
    )

    nodes, relationships, rejections = _filter_unsupported_entity_nodes(
        [card], [node], []
    )

    assert nodes == []
    assert relationships == []
    assert rejections[0]["relation_type"] == "entity_support"
