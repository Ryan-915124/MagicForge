from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from knowledge.evidence import (
    ApplicationOrigin,
    ClaimRole,
    EvidenceClass,
    MagicDomain,
    MechanismStatus,
)
from knowledge.governance import (
    ClaimEligibility,
    ExtractionPermission,
    ReviewStatus,
    SensitiveInformationLevel,
    StoragePermission,
)
from knowledge.models import EntityType
from research.citation.manager import CitationManager
from research.extraction.extractor import ResearchKnowledgeExtractor
from research.extraction.models import (
    ExtractedClaim,
    ExtractedEntity,
    ResearchExtractionResult,
)
from research.extraction.production_bridge import (
    ProductionBridgeError,
    build_production_claim_submissions,
)
from research.models import (
    ContentAccess,
    DiscoveryProvider,
    ResearchCandidate,
    SearchProvenance,
    SourceCategory,
)
from research.review.source_repository import InMemorySourceApprovalRepository
from research.review.source_service import SourceApprovalService
from research.review.workflow_models import (
    ExtractionProducer,
    ExtractionProvenanceInput,
)


class FakeGLM:
    model = "glm-4.7"

    def __init__(self, response: ResearchExtractionResult) -> None:
        self.response = response

    def generate_structured(self, _prompt: str, _response_model: type, **_: Any):
        return self.response


def _proposal():
    source_text = (
        "Attentional selection may guide what spectators notice. "
        "Selective attention may shape the experience."
    )
    candidate = ResearchCandidate(
        title="Attention in Magic",
        url="https://doi.org/10.1234/magic.bridge",
        authors=["A. Researcher"],
        published_year=2026,
        venue="Journal of Magic Cognition",
        doi="10.1234/magic.bridge",
        content=source_text,
        content_access=ContentAccess.WEB_EXTRACT,
        source_category=SourceCategory.ACADEMIC,
        provenance=[
            SearchProvenance(
                provider=DiscoveryProvider.EXA,
                tool_name="web_search_exa",
                query="attention in magic",
                rank=1,
            )
        ],
    )
    manager = CitationManager()
    citation = manager.mark_verified(
        manager.from_candidate(candidate),
        verifier="Crossref metadata check",
        verification_source="https://api.crossref.org/works/10.1234/magic.bridge",
    )
    approvals = SourceApprovalService(InMemorySourceApprovalRepository())
    pending = approvals.submit(candidate, citation)
    approved = approvals.approve(
        pending.id,
        reviewer="Ryan Chen",
        reason="Exact source and extraction permission checked.",
        claim_eligibility=ClaimEligibility.ELIGIBLE,
        extraction_permission=ExtractionPermission.FULL_TEXT,
        storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
        sensitive_information_level=SensitiveInformationLevel.CONTROLLED,
    )
    claim = "Attentional selection may guide what spectators notice."
    result = ResearchExtractionResult(
        magic_category="misdirection",
        entities=[
            ExtractedEntity(
                type=EntityType.COGNITIVE_MECHANISM,
                name="Attentional selection",
                evidence_excerpt=claim,
                supporting_claims=[claim],
                confidence=0.9,
            ),
            ExtractedEntity(
                type=EntityType.PSYCHOLOGY_PRINCIPLE,
                name="Selective attention",
                evidence_excerpt="Selective attention may shape the experience.",
                supporting_claims=[claim],
                confidence=0.85,
            ),
        ],
        claims=[
            ExtractedClaim(
                statement=claim,
                evidence_excerpt=claim,
                locator="https://doi.org/10.1234/magic.bridge#results",
                confidence=0.88,
                claim_role=ClaimRole.RESULT,
                evidence_class=EvidenceClass.CONTROLLED_EXPERIMENT,
                applicable_domains=[MagicDomain.CLOSE_UP, MagicDomain.THEORY],
                ontology_paths=["psychology.attention.selective_attention"],
                topic_tags=["attention", "misdirection"],
                mechanism_names=["Attentional selection"],
                principle_names=["Selective attention"],
                magic_application="Direct attention before the secret action.",
                application_origin=ApplicationOrigin.SOURCE_STATED,
                limitations=["One controlled viewing context."],
                population_context="Adult spectators.",
                performance_context="A close-up demonstration.",
            )
        ],
    )
    return ResearchKnowledgeExtractor(FakeGLM(result), chunk_size=500).extract_candidate(
        candidate, citation, approved
    )


def _provenance(proposal) -> ExtractionProvenanceInput:
    return ExtractionProvenanceInput(
        producer=ExtractionProducer.GLM,
        extractor="ResearchKnowledgeExtractor",
        extraction_schema_version=proposal.schema_version,
        run_id=proposal.extraction_run_id,
        llm_provider="GLM",
        model="glm-4.7",
        tool_version="production-bridge-0.1",
    )


def test_bridge_preserves_claim_fields_and_leaves_entities_unresolved() -> None:
    proposal = _proposal()

    first = build_production_claim_submissions(
        proposal, extraction_provenance=_provenance(proposal)
    )
    second = build_production_claim_submissions(
        proposal, extraction_provenance=_provenance(proposal)
    )

    assert len(first) == 1
    submission = first[0]
    command = submission.command
    card = proposal.evidence_cards[0]
    assert command.source_version_id == UUID(proposal.source_approval.source_version_id)
    assert command.claim == card.claim
    assert command.claim_role == card.claim_role
    assert command.proposed_evidence_class == card.evidence_class
    assert command.locator == card.locator
    assert command.evidence_excerpt == card.evidence_excerpt
    assert command.proposed_limitations == card.limitations
    assert command.population_context == card.population_context
    assert command.performance_context == card.performance_context
    assert command.magic_application == card.magic_application
    assert command.application_origin == card.application_origin
    assert command.extraction_provenance.llm_provider == "GLM"
    assert command.extraction_provenance.run_id == proposal.extraction_run_id
    assert command.mechanism_status == MechanismStatus.UNRESOLVED
    assert command.mechanism_ids == []
    assert command.principle_ids == []
    assert [item.name for item in submission.unresolved_mechanisms] == [
        "Attentional selection"
    ]
    assert [item.name for item in submission.unresolved_principles] == [
        "Selective attention"
    ]
    assert submission.idempotency_key == second[0].idempotency_key
    assert submission.idempotency_key.startswith("glm-claim-")


def test_bridge_links_only_explicitly_resolved_canonical_entities() -> None:
    proposal = _proposal()
    extraction_ids = {
        item.entity.type: item.entity.id for item in proposal.entity_proposals
    }
    canonical_mechanism = uuid4()
    canonical_principle = uuid4()

    submission = build_production_claim_submissions(
        proposal,
        extraction_provenance=_provenance(proposal),
        resolved_entity_ids={
            extraction_ids[EntityType.COGNITIVE_MECHANISM]: canonical_mechanism,
            extraction_ids[EntityType.PSYCHOLOGY_PRINCIPLE]: canonical_principle,
        },
    )[0]

    assert submission.command.mechanism_status == MechanismStatus.LINKED
    assert submission.command.mechanism_ids == [canonical_mechanism]
    assert submission.command.principle_ids == [canonical_principle]
    assert submission.unresolved_mechanisms == ()
    assert submission.unresolved_principles == ()


@pytest.mark.parametrize("mutation", ["context_only", "non_glm", "source_mismatch"])
def test_bridge_rejects_ineligible_or_unbound_cards(mutation: str) -> None:
    proposal = _proposal()
    card = proposal.evidence_cards[0]
    if mutation == "context_only":
        card = card.model_copy(update={"claim_role": ClaimRole.CONTEXT_ONLY})
    elif mutation == "non_glm":
        card = card.model_copy(update={"created_by": "pipeline"})
    else:
        source = card.source.model_copy(update={"source_version_id": str(uuid4())})
        card = card.model_copy(update={"source": source})
    proposal = proposal.model_copy(update={"evidence_cards": [card]})

    with pytest.raises(ProductionBridgeError):
        build_production_claim_submissions(
            proposal, extraction_provenance=_provenance(proposal)
        )


def test_bridge_rejects_source_without_current_extraction_approval() -> None:
    proposal = _proposal()
    approval = proposal.source_approval.model_copy(
        update={"status": ReviewStatus.PENDING}
    )
    proposal = proposal.model_copy(update={"source_approval": approval})

    with pytest.raises(ProductionBridgeError, match="not approved"):
        build_production_claim_submissions(
            proposal, extraction_provenance=_provenance(proposal)
        )


def test_bridge_rejects_non_glm_missing_or_mismatched_run_provenance() -> None:
    proposal = _proposal()
    invalid = [
        ExtractionProvenanceInput.model_construct(
            producer=ExtractionProducer.PIPELINE,
            extractor="pipeline",
            extraction_schema_version=proposal.schema_version,
            run_id=proposal.extraction_run_id,
            llm_provider="GLM",
            model="glm-4.7",
            tool_version=None,
        ),
        ExtractionProvenanceInput.model_construct(
            producer=ExtractionProducer.GLM,
            extractor="ResearchKnowledgeExtractor",
            extraction_schema_version=proposal.schema_version,
            run_id=None,
            llm_provider="GLM",
            model="glm-4.7",
            tool_version=None,
        ),
        _provenance(proposal).model_copy(update={"run_id": str(uuid4())}),
        _provenance(proposal).model_copy(
            update={"extraction_schema_version": "extraction-legacy"}
        ),
    ]

    for provenance in invalid:
        with pytest.raises(ProductionBridgeError):
            build_production_claim_submissions(
                proposal, extraction_provenance=provenance
            )
