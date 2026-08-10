"""Isolated Checkpoint 5 governance chain; no real GLM or persistent Qdrant."""

from __future__ import annotations

from uuid import uuid4

import pytest
from qdrant_client import QdrantClient

from app.config import Settings
from app.knowledge_read_model import KnowledgeReadModelError
from app.production_read_model import ProductionKnowledgeReadModel
from app.runtime import RuntimeInitializationError, build_runtime_services
from knowledge.governance import MagicForgeMode
from research.review.mapping_service import ProductionMappingService
from research.review.storage_workflow_models import (
    CorpusActivationCommand,
    ManifestAuthorizationCommand,
    ManifestBuildCommand,
    ManifestIngestionCommand,
)
from research.review.storage_workflow_service import ProductionStorageWorkflowService
from retrieval.interfaces import KnowledgeSearchFilter
from retrieval.qdrant_service import QdrantService
from persistence.storage_models import CorpusActivationState, CorpusVersionRecord
from security.policy import retrieval_authorization_for_actor
from test_governance_workflow import (
    NOW,
    CLAIM_TEXT,
    _claim_command,
    _claim_review_command,
    _register_and_approve_source,
    governance,
)
from test_mapping_workflow import _approve, _entity_command


class TinyEmbeddings:
    dimension = 3

    def embed_documents(self, texts):
        return [
            [float((index % 3) == 0), float((index % 3) == 1), 1.0]
            for index, _text in enumerate(texts)
        ]

    def embed_query(self, _text):
        return [0.0, 0.0, 1.0]


class NoNetworkGLM:
    model = "glm-test-no-network"

    def generate(self, _prompt, *, system_prompt=None, **_kwargs):
        assert system_prompt
        return "An isolated governed answer."


def test_complete_governance_chain_activates_one_shared_production_corpus(
    governance,
) -> None:
    # The governance fixture provisions an administrator, then independently
    # creates authenticated reviewer and operator accounts with live sessions.
    _source_id, source_version_id = _register_and_approve_source(
        governance, slug="checkpoint-five-e2e"
    )
    claim_command = _claim_command(
        source_version_id,
        claim="Participants may overlook an unexpected action in a demonstration.",
    ).model_copy(update={"evidence_excerpt": CLAIM_TEXT})
    claim = governance.service.submit_claim(
        governance.operator,
        claim_command,
        idempotency_key="checkpoint-five-claim-submit",
    )
    claim_review = governance.service.review_claim(
        governance.reviewer,
        claim.id,
        _claim_review_command(governance.reviewer.username),
        idempotency_key="checkpoint-five-claim-review",
    )
    assert claim_review.evidence_version_id is not None

    mapping = ProductionMappingService(
        governance.database.session_factory, clock=lambda: NOW
    )
    proposal = mapping.submit_entity(
        governance.operator,
        _entity_command(claim_review.evidence_version_id),
        idempotency_key="checkpoint-five-mapping-submit",
    )
    mapping_review = mapping.review_mapping(
        governance.reviewer,
        proposal.id,
        _approve(governance.reviewer),
        idempotency_key="checkpoint-five-mapping-review",
    )
    assert mapping_review.knowledge_node_version_id is not None

    collection = f"magicforge_cp4_test_{uuid4().hex}"
    corpus_id = uuid4()
    qdrant_client = QdrantClient(location=":memory:")
    writer = QdrantService(
        "embedded",
        collection,
        TinyEmbeddings(),
        qdrant_client,
        mode=MagicForgeMode.PRODUCTION,
        production_writes_enabled=True,
    )
    storage = ProductionStorageWorkflowService(
        governance.database.session_factory,
        writer=writer,
        allow_temporary_qdrant_writes=True,
        persistent_production_write_approved=False,
        vector_size=3,
        vector_distance="cosine",
        clock=lambda: NOW,
    )
    built = storage.build_manifest(
        governance.operator,
        ManifestBuildCommand(
            corpus_id=corpus_id,
            collection_name=collection,
            evidence_version_ids=[claim_review.evidence_version_id],
            knowledge_node_version_ids=[
                mapping_review.knowledge_node_version_id
            ],
        ),
        idempotency_key="checkpoint-five-manifest-build",
    )
    authorized = storage.authorize_manifest(
        governance.operator,
        built.id,
        ManifestAuthorizationCommand(
            reason="Authorize exact reviewed versions for the isolated test corpus."
        ),
        idempotency_key="checkpoint-five-manifest-authorize",
    )
    ingestion = storage.ingest_manifest(
        governance.operator,
        authorized.id,
        ManifestIngestionCommand(
            reason="Ingest only into the isolated in-memory Qdrant collection."
        ),
        idempotency_key="checkpoint-five-manifest-ingest",
    )
    assert ingestion.point_count == 2
    active = storage.activate_corpus(
        governance.operator,
        corpus_id,
        CorpusActivationCommand(
            runtime_scope="production",
            reason="Activate the receipt-backed isolated corpus for read tests.",
        ),
        idempotency_key="checkpoint-five-corpus-activate",
    )
    assert active.activation_state == "active"

    settings = Settings(
        magicforge_mode=MagicForgeMode.PRODUCTION,
        database_url="postgresql+psycopg://unused:unused@invalid/magicforge",
        qdrant_url="http://unused",
        qdrant_collection_name=collection,
        active_corpus_id=str(corpus_id),
        active_corpus_manifest_schema="manifest-0.2",
        active_qdrant_storage_kind="remote",
        embedding_dimension=3,
    )
    runtime = build_runtime_services(
        settings,
        session_factory=governance.database.session_factory,
        qdrant_client_factory=lambda _corpus: qdrant_client,
        embedding_factory=lambda _settings: TinyEmbeddings(),
        glm_factory=lambda _settings: NoNetworkGLM(),
    )
    try:
        assert runtime.active_corpus.corpus_id == str(corpus_id)
        assert runtime.active_corpus.collection_name == collection
        assert runtime.magicforge_service.active_corpus is runtime.active_corpus
        assert runtime.knowledge_read_model.active_corpus is runtime.active_corpus
        assert runtime.research_console_read_model.active_corpus is runtime.active_corpus
        assert isinstance(runtime.knowledge_read_model, ProductionKnowledgeReadModel)

        authorization = retrieval_authorization_for_actor(governance.operator)
        browser_page = runtime.knowledge_read_model.search(
            query="Participants",
            limit=10,
            filters=KnowledgeSearchFilter(),
            authorization=authorization,
        )
        assert browser_page.projection.run_id == str(corpus_id)
        assert browser_page.evidence_cards

        answer, chat_sources = runtime.magicforge_service.assistant(
            "Why might participants fail to notice the action?",
            authorization=authorization,
        )
        assert answer == "An isolated governed answer."
        assert chat_sources
        assert all(
            item.payload["corpus_id"] == str(corpus_id)
            for item in chat_sources
        )
        assert all(
            item.payload["storage_manifest_id"] == str(authorized.id)
            for item in chat_sources
        )

        # Activation and approval are live governance state, not process-local
        # cache state.  Once the active corpus is invalidated, both browser and
        # Chat paths must fail closed until a new runtime is constructed.
        with governance.database.session_factory() as session:
            corpus = session.get(CorpusVersionRecord, corpus_id)
            assert corpus is not None
            corpus.activation_state = CorpusActivationState.INACTIVE
            session.commit()

        with pytest.raises(KnowledgeReadModelError):
            runtime.knowledge_read_model.search(
                query="Participants",
                limit=10,
                filters=KnowledgeSearchFilter(),
                authorization=authorization,
            )
        with pytest.raises(RuntimeInitializationError) as stale_chat:
            runtime.magicforge_service.assistant(
                "Why might participants fail to notice the action?",
                authorization=authorization,
            )
        assert stale_chat.value.code == "active_corpus_stale"
        with pytest.raises(RuntimeInitializationError):
            runtime.validate_current()
    finally:
        runtime.close()
