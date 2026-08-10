from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import Settings
from knowledge.bootstrap import (
    BOOTSTRAP_COLLECTION_NAME,
    BootstrapProjectionBuilder,
    BootstrapStorageManifest,
)
from knowledge.evidence import (
    ApplicationOrigin,
    ClaimRole,
    EvidenceClass,
    MagicDomain,
)
from knowledge.governance import MagicForgeMode, ReviewStatus
from knowledge.models import EntityType, RelationType
from research.bootstrap.service import (
    BootstrapKnowledgeAssembler,
    BootstrapSourceRegistrar,
)
from research.citation.manager import CitationManager
from research.extraction.extractor import (
    ResearchExtractionError,
    ResearchKnowledgeExtractor,
)
from research.extraction.models import (
    ExtractedClaim,
    ExtractedEntity,
    ExtractedRelationship,
    ResearchExtractionResult,
)
from research.models import (
    ContentAccess,
    DiscoveryProvider,
    ResearchCandidate,
    SearchProvenance,
    SourceCategory,
)
from retrieval.qdrant_service import QdrantService


class FakeBootstrapGLM:
    model = "glm-test"

    def generate(self, *_args, **_kwargs):
        return self.generate_structured().model_dump_json()

    def generate_structured(self, *_args, **_kwargs):
        statement = "Limited attentional capacity explains why selective attention can reduce detection of an unexpected action."
        excerpt = "Limited attentional capacity explains why observers sometimes failed to report the unexpected action."
        return ResearchExtractionResult(
            magic_category="theory",
            claims=[
                ExtractedClaim(
                    statement=statement,
                    evidence_excerpt=excerpt,
                    confidence=0.9,
                    claim_role=ClaimRole.RESULT,
                    evidence_class=EvidenceClass.CONTROLLED_EXPERIMENT,
                    applicable_domains=[MagicDomain.THEORY],
                    ontology_paths=["Psychology.Attention.SelectiveAttention"],
                    mechanism_names=["Limited attentional capacity"],
                    principle_names=["Selective attention"],
                    magic_application="Treat attention management as a hypothesis, not proof of concealment.",
                    application_origin=ApplicationOrigin.SOURCE_STATED,
                    limitations=["Bootstrap extraction has not been human reviewed."],
                )
            ],
            entities=[
                ExtractedEntity(
                    type=EntityType.PSYCHOLOGY_PRINCIPLE,
                    name="Selective attention",
                    evidence_excerpt=excerpt,
                    supporting_claims=[statement],
                    confidence=0.9,
                ),
                ExtractedEntity(
                    type=EntityType.COGNITIVE_MECHANISM,
                    name="Limited attentional capacity",
                    evidence_excerpt=excerpt,
                    supporting_claims=[statement],
                    confidence=0.8,
                ),
            ],
            relationships=[
                ExtractedRelationship(
                    source_name="Limited attentional capacity",
                    target_name="Selective attention",
                    type=RelationType.EXPLAINS,
                    evidence_excerpt=excerpt,
                    supporting_claims=[statement],
                    confidence=0.8,
                )
            ],
        )


def _candidate() -> ResearchCandidate:
    return ResearchCandidate(
        title="Attention and Magic",
        url="https://example.org/attention",
        authors=["A. Researcher"],
        published_year=2024,
        venue="Example Journal",
        doi="10.1234/example.1",
        content="Limited attentional capacity explains why observers sometimes failed to report the unexpected action.",
        content_access=ContentAccess.WEB_EXTRACT,
        source_category=SourceCategory.ACADEMIC,
        provenance=[
            SearchProvenance(
                provider=DiscoveryProvider.EXA,
                tool_name="web_fetch_exa",
                query="magic attention",
            )
        ],
    )


def test_production_is_default_and_bootstrap_collection_is_isolated() -> None:
    assert Settings().magicforge_mode == MagicForgeMode.PRODUCTION
    assert Settings().active_qdrant_collection_name == "magicforge_knowledge_v01"
    bootstrap = Settings(magicforge_mode=MagicForgeMode.BOOTSTRAP)
    assert bootstrap.active_qdrant_collection_name == BOOTSTRAP_COLLECTION_NAME


def test_bootstrap_extracts_without_making_human_approval_claims() -> None:
    candidate = _candidate()
    citation = CitationManager().from_candidate(candidate)
    approval = BootstrapSourceRegistrar(MagicForgeMode.BOOTSTRAP).register(
        candidate, citation
    )
    assert approval.status == ReviewStatus.BOOTSTRAP_PENDING_HUMAN_REVIEW
    assert approval.reviewer is None

    proposal = ResearchKnowledgeExtractor(
        FakeBootstrapGLM(), mode=MagicForgeMode.BOOTSTRAP
    ).extract_candidate(candidate, citation, approval)
    card = proposal.evidence_cards[0]
    assert card.review.review_status == ReviewStatus.BOOTSTRAP_GENERATED
    assert card.review.approved is False
    assert card.confidence is not None
    assert card.contradiction_status.value == "not_checked"

    with pytest.raises(ResearchExtractionError, match="named-human"):
        ResearchKnowledgeExtractor(FakeBootstrapGLM()).extract_candidate(
            candidate, citation, approval
        )


def test_bootstrap_projection_and_manifest_never_claim_human_verification() -> None:
    candidate = _candidate()
    citation = CitationManager().from_candidate(candidate)
    approval = BootstrapSourceRegistrar(MagicForgeMode.BOOTSTRAP).register(
        candidate, citation
    )
    proposal = ResearchKnowledgeExtractor(
        FakeBootstrapGLM(), mode=MagicForgeMode.BOOTSTRAP
    ).extract_candidate(candidate, citation, approval)
    nodes, relationships = BootstrapKnowledgeAssembler().assemble(proposal)
    cards = {card.id: card for card in proposal.evidence_cards}
    builder = BootstrapProjectionBuilder()
    projections = [
        builder.from_evidence_card(proposal.evidence_cards[0]),
        builder.from_node(nodes[0], cards),
        builder.from_relationship(relationships[0], cards),
    ]
    manifest = BootstrapStorageManifest(projections=projections)

    assert manifest.collection_name == BOOTSTRAP_COLLECTION_NAME
    assert manifest.human_authorized is False
    for projection in projections:
        payload = projection.to_payload(
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
        )
        assert payload["bootstrap_generated"] is True
        assert payload["human_verified"] is False
        assert payload["approved"] is False
        assert payload["review_status"] == "bootstrap"

    with pytest.raises(ValidationError):
        BootstrapStorageManifest(
            collection_name="magicforge_knowledge_v01",
            projections=projections,
        )


def test_bootstrap_retrieval_filter_does_not_weaken_production_filter() -> None:
    from persistence.models import RoleName
    from security.policy import (
        AuthenticatedActor,
        bootstrap_anonymous_retrieval_authorization,
        retrieval_authorization_for_actor,
    )

    production = QdrantService._build_filter(
        None,
        retrieval_authorization_for_actor(
            AuthenticatedActor(
                user_id=uuid4(),
                username="reader",
                roles=frozenset({RoleName.READER}),
            )
        ),
        corpus_id=str(uuid4()),
        manifest_id=str(uuid4()),
        manifest_hash="a" * 64,
        projection_schema="qdrant-0.2",
    )
    assert {condition.key for condition in production.must}.issuperset(
        {"approved", "claim_eligibility", "storage_permission", "review_status"}
    )

    bootstrap = QdrantService._build_filter(
        None,
        bootstrap_anonymous_retrieval_authorization(),
        mode=MagicForgeMode.BOOTSTRAP,
    )
    keys = {condition.key for condition in bootstrap.must}
    assert {"bootstrap_generated", "human_verified", "review_status"}.issubset(keys)
    assert "approved" not in keys
