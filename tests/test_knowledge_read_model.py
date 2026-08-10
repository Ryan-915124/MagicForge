from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app.config import Settings
from app.knowledge_read_model import KnowledgeReadModelError, ProjectedKnowledgeReadModel
from app.runtime_corpus import load_active_corpus
from knowledge.governance import MagicForgeMode
from persistence.models import RoleName
from retrieval.interfaces import (
    KnowledgeSearchFilter,
    RetrievalAuthorizationRequiredError,
)
from security.policy import (
    AuthenticatedActor,
    bootstrap_anonymous_retrieval_authorization,
    retrieval_authorization_for_actor,
)


RUN_PATH = Path("research/runs/bootstrap-002")
BOOTSTRAP_AUTHORIZATION = bootstrap_anonymous_retrieval_authorization()


def _active_corpus():
    return load_active_corpus(
        Settings(
            magicforge_mode=MagicForgeMode.BOOTSTRAP,
            active_corpus_root=str(RUN_PATH),
            active_corpus_id="bootstrap-002",
            active_corpus_manifest_path=str(
                RUN_PATH / "qdrant_manifest/bootstrap-manifest-v03.json"
            ),
            active_corpus_receipt_path=str(
                RUN_PATH / "qdrant_manifest/ingestion-receipt-v03.json"
            ),
            active_corpus_manifest_schema="bootstrap-manifest-0.2",
            active_qdrant_storage_kind="local",
            active_qdrant_local_path=str(RUN_PATH / "qdrant_storage_v03"),
        )
    )


def test_read_model_matches_projected_manifest_counts() -> None:
    active_corpus = _active_corpus()
    model = ProjectedKnowledgeReadModel(active_corpus)
    model.validate()

    assert model.active_corpus is active_corpus
    assert model.summary.collection == "magicforge_bootstrap_v03"
    assert model.summary.run_id == active_corpus.corpus_id
    assert model.summary.manifest_id == active_corpus.manifest_id
    assert model.summary.sources == 219
    assert model.summary.evidence_cards == 632
    assert model.summary.knowledge_nodes == 158
    assert model.summary.relationships == 7
    assert model.summary.renderable_relationships == 4
    assert model.summary.qdrant_points == 797
    assert model.summary.human_verified is False

    assert model.statistics.sources_with_projected_knowledge == 185
    assert model.statistics.source_categories == {
        "academic": 88,
        "practitioner": 131,
    }
    assert model.statistics.artifact_types == {
        "evidence_card": 632,
        "knowledge_node": 158,
        "relationship": 7,
    }
    assert model.statistics.domain_memberships == {
        "card": 62,
        "close-up": 102,
        "mentalism": 74,
        "stage": 127,
        "theory": 558,
    }
    assert model.statistics.knowledge_origins == {
        "expert_practice": 444,
        "personal_interpretation": 56,
        "scientific_evidence": 297,
    }
    assert model.statistics.human_verified_points == 0
    assert model.statistics.contradiction_checks_pending == 632
    assert model.statistics.pending_human_review_sources == 219
    assert model.statistics.production_collection_touched is False


def test_default_graph_returns_only_real_projected_entities_and_edges() -> None:
    model = ProjectedKnowledgeReadModel(_active_corpus())
    page = model.search(
        "",
        60,
        KnowledgeSearchFilter(),
        authorization=BOOTSTRAP_AUTHORIZATION,
    )

    assert len(page.nodes) == 60
    assert len(page.relationships) == 4
    entity_ids = {item.entity.id for item in page.nodes}
    assert all(item.source_id in entity_ids for item in page.relationships)
    assert all(item.target_id in entity_ids for item in page.relationships)
    assert all(item.bootstrap_generated for item in page.nodes)
    assert all(not item.human_verified for item in page.nodes)


def test_evidence_search_uses_projected_evidence_cards() -> None:
    model = ProjectedKnowledgeReadModel(_active_corpus())
    page = model.search(
        "attention",
        20,
        KnowledgeSearchFilter(knowledge_types=["evidence"]),
        authorization=BOOTSTRAP_AUTHORIZATION,
    )

    assert page.evidence_cards
    assert not page.nodes
    assert all(
        card.id == result.payload["artifact_id"]
        for card, result in zip(page.evidence_cards, page.results, strict=True)
    )


def test_read_model_requires_authorization_before_loading_or_selecting() -> None:
    model = ProjectedKnowledgeReadModel(_active_corpus())

    with pytest.raises(RetrievalAuthorizationRequiredError):
        model.search("attention", 5, KnowledgeSearchFilter())
    with pytest.raises(RetrievalAuthorizationRequiredError):
        model.get_node("anything")
    with pytest.raises(RetrievalAuthorizationRequiredError):
        model.get_evidence("anything")

    assert model._loaded is False


def test_read_model_filters_controlled_and_secret_projection_before_return() -> None:
    model = ProjectedKnowledgeReadModel(_active_corpus())
    model.validate()
    card = model._evidence_cards[0]
    projection = dict(model._evidence_projection[card.id])
    projection.update(
        sensitive_information_level="secret_method",
        secret_exposure_rank=2,
    )
    model._evidence_projection[card.id] = projection
    reader = retrieval_authorization_for_actor(
        AuthenticatedActor(
            user_id=uuid4(),
            username="reader",
            roles=frozenset({RoleName.READER}),
        )
    )
    reviewer = retrieval_authorization_for_actor(
        AuthenticatedActor(
            user_id=uuid4(),
            username="reviewer",
            roles=frozenset({RoleName.REVIEWER}),
        )
    )

    assert model.get_evidence(card.id, authorization=reader) is None
    assert model.get_evidence(card.id, authorization=reviewer) is card


def test_read_model_rejects_a_production_corpus() -> None:
    production = replace(
        _active_corpus(),
        mode=MagicForgeMode.PRODUCTION,
        bootstrap_generated=False,
        human_verified=True,
    )

    with pytest.raises(KnowledgeReadModelError, match="requires a Bootstrap"):
        ProjectedKnowledgeReadModel(production)


def test_validate_rejects_descriptor_identity_drift() -> None:
    mismatched = replace(
        _active_corpus(),
        collection_name="magicforge_bootstrap_wrong",
    )
    model = ProjectedKnowledgeReadModel(mismatched)

    with pytest.raises(
        KnowledgeReadModelError,
        match="collection does not match the active corpus",
    ):
        model.validate()
