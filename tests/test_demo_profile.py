from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from qdrant_client import QdrantClient

import app.main as main_module
from app.config import (
    PROJECT_ROOT,
    MagicForgeProfile,
    Settings,
    SettingsConfigurationError,
)
from app.main import create_app
from app.runtime import RuntimeInitializationError, build_runtime_services
from app.runtime_corpus import load_active_corpus
from knowledge.demo import load_demo_bundle
from knowledge.governance import MagicForgeMode
from retrieval.demo import (
    DeterministicDemoEmbeddingProvider,
    seed_demo_collection,
)
from retrieval.interfaces import KnowledgeSearchFilter
from retrieval.qdrant_service import QdrantServiceError
from security.policy import (
    bootstrap_anonymous_actor,
    retrieval_authorization_for_actor,
)


DEMO_PATH = PROJECT_ROOT / "data/demo/corpus.json"


def _demo_settings(**changes) -> Settings:
    settings = Settings(
        magicforge_profile=MagicForgeProfile.DEMO,
        magicforge_mode=MagicForgeMode.BOOTSTRAP,
        glm_api_key="must-not-be-used",
        database_url="postgresql+psycopg://must-not-be-used",
        bootstrap_allow_anonymous_reads=True,
        embedding_dimension=64,
        retrieval_limit=5,
        active_corpus_root=str(PROJECT_ROOT / "research/runs/bootstrap-002"),
        active_corpus_manifest_path=str(
            PROJECT_ROOT
            / "research/runs/bootstrap-002/qdrant_manifest/bootstrap-manifest-v03.json"
        ),
        active_corpus_receipt_path=str(
            PROJECT_ROOT
            / "research/runs/bootstrap-002/qdrant_manifest/ingestion-receipt-v03.json"
        ),
    )
    return replace(settings, **changes)


def test_unknown_profile_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("MAGICFORGE_PROFILE", "demonstration")

    with pytest.raises(SettingsConfigurationError, match="demo, development, or production"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("profile", "expected_mode"),
    [
        ("development", MagicForgeMode.BOOTSTRAP),
        ("production", MagicForgeMode.PRODUCTION),
    ],
)
def test_named_profiles_are_the_mode_source_of_truth(
    monkeypatch, profile: str, expected_mode: MagicForgeMode
) -> None:
    root = PROJECT_ROOT / "research/runs/bootstrap-002"
    monkeypatch.setenv("MAGICFORGE_PROFILE", profile)
    monkeypatch.setenv(
        "MAGICFORGE_MODE",
        "production" if profile == "development" else "bootstrap",
    )
    monkeypatch.setenv("BOOTSTRAP_ALLOW_ANONYMOUS_READS", "false")
    monkeypatch.setenv("ACTIVE_CORPUS_ROOT", str(root))
    monkeypatch.setenv("KNOWLEDGE_RUN_PATH", str(root))

    settings = Settings.from_env()

    assert settings.effective_profile == MagicForgeProfile(profile)
    assert settings.magicforge_mode == expected_mode


def test_demo_profile_ignores_private_runtime_and_credentials(monkeypatch) -> None:
    monkeypatch.setenv("MAGICFORGE_PROFILE", "demo")
    monkeypatch.setenv("MAGICFORGE_MODE", "production")
    monkeypatch.setenv("GLM_API_KEY", "must-not-be-loaded")
    monkeypatch.setenv("GLM_TIMEOUT_SECONDS", "not-used")
    monkeypatch.setenv("PRODUCTION_GLM_EXTRACTION_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://must-not-be-loaded")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITES_ENABLED", "true")
    monkeypatch.setenv("PRODUCTION_QDRANT_WRITE_MANIFEST_ID", "not-used")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "not-used")
    monkeypatch.setenv(
        "ACTIVE_CORPUS_ROOT", str(PROJECT_ROOT / "research/runs/bootstrap-002")
    )
    monkeypatch.setenv(
        "KNOWLEDGE_RUN_PATH", str(PROJECT_ROOT / "research/runs/bootstrap-002")
    )

    settings = Settings.from_env()

    assert settings.effective_profile == MagicForgeProfile.DEMO
    assert settings.magicforge_mode == MagicForgeMode.BOOTSTRAP
    assert settings.glm_api_key == ""
    assert settings.database_url == ""
    assert settings.production_glm_extraction_enabled is False
    assert settings.production_qdrant_writes_enabled is False
    assert settings.embedding_model == "deterministic-demo-hash"
    assert settings.embedding_dimension == 128
    assert Path(settings.active_corpus_root) == PROJECT_ROOT / "data/demo"
    assert Path(settings.knowledge_run_path) == PROJECT_ROOT / "data/demo"
    assert settings.active_qdrant_collection_name == "magicforge_demo_v01"
    assert settings.anonymous_product_reads_enabled is True


def test_demo_corpus_loader_never_reads_research_runs(monkeypatch) -> None:
    observed: list[Path] = []
    original = Path.read_text

    def guarded_read_text(path: Path, *args, **kwargs):
        resolved = path.resolve()
        observed.append(resolved)
        assert PROJECT_ROOT / "research/runs" not in resolved.parents
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    corpus = load_active_corpus(_demo_settings())

    assert observed == [DEMO_PATH.resolve()]
    assert corpus.artifact_root == (PROJECT_ROOT / "data/demo").resolve()
    assert corpus.collection_name == "magicforge_demo_v01"


def test_demo_materialization_has_stable_identifiers() -> None:
    first = load_demo_bundle(DEMO_PATH)
    second = load_demo_bundle(DEMO_PATH)

    assert first.spec.synthetic is True
    assert first.spec.self_authored is True
    assert first.spec.redistribution_allowed is True
    assert all(item.synthetic for item in first.sources)
    assert all(item.self_authored for item in first.sources)
    assert all(item.redistribution_allowed for item in first.sources)
    assert len(first.sources) >= 5
    assert len(first.claims) >= 10
    assert len(first.evidence_cards) >= 8
    assert len(first.nodes) >= 8
    assert len(first.relationships) >= 8
    node_names = {item.entity.name for item in first.nodes}
    assert {
        "Misdirection",
        "Selective Attention",
        "Working Memory",
        "Prediction Error",
        "Audience Management",
    }.issubset(node_names)
    assert first.manifest_id == second.manifest_id
    assert first.manifest_hash == second.manifest_hash
    assert first.receipt_id == second.receipt_id
    assert [item.id for item in first.sources] == [item.id for item in second.sources]
    assert [item.id for item in first.claims] == [item.id for item in second.claims]
    assert [item.id for item in first.evidence_cards] == [
        item.id for item in second.evidence_cards
    ]
    assert [item.id for item in first.nodes] == [item.id for item in second.nodes]
    assert [item.id for item in first.relationships] == [
        item.id for item in second.relationships
    ]
    assert first.expected_point_ids == second.expected_point_ids
    assert first.payload_checksums == second.payload_checksums


def test_demo_seed_is_idempotent_and_exact() -> None:
    bundle = load_demo_bundle(DEMO_PATH)
    embeddings = DeterministicDemoEmbeddingProvider(64)
    client = QdrantClient(location=":memory:")
    try:
        assert seed_demo_collection(client, bundle, embeddings) is True
        assert seed_demo_collection(client, bundle, embeddings) is False
        assert (
            client.count(collection_name=bundle.spec.collection_name, exact=True).count
            == len(bundle.projections)
        )
    finally:
        client.close()


def test_demo_runtime_is_offline_and_returns_graph_and_retrieval() -> None:
    def external_dependency_forbidden(_settings):
        raise AssertionError("Demo attempted to construct an external model")

    runtime = build_runtime_services(
        _demo_settings(),
        embedding_factory=external_dependency_forbidden,
        glm_factory=external_dependency_forbidden,
    )
    authorization = retrieval_authorization_for_actor(bootstrap_anonymous_actor())
    try:
        page = runtime.knowledge_read_model.search(
            query="selective attention",
            limit=20,
            filters=KnowledgeSearchFilter(),
            authorization=authorization,
        )
        semantic_results = runtime.retriever.search_documents(
            "selective attention",
            limit=5,
            authorization=authorization,
        )
        answer, answer_sources = runtime.magicforge_service.assistant(
            "How is attention represented?",
            authorization=authorization,
        )
        analysis, analysis_sources = runtime.magicforge_service.analyze(
            "A token appears absent after a visible gesture.",
            authorization=authorization,
        )

        assert page.evidence_cards
        assert page.nodes
        assert page.relationships
        assert semantic_results
        assert answer_sources
        assert analysis_sources
        assert analysis.overall_score == 3.0
        assert "generated offline" in answer
        assert runtime.magicforge_service.llm.model == "deterministic-demo"
        with pytest.raises(QdrantServiceError):
            runtime.retriever.create_collection()
    finally:
        runtime.close()


@pytest.mark.anyio
async def test_demo_api_starts_without_database_or_glm(monkeypatch) -> None:
    async def dispatch(operation, *args):
        return operation(*args)

    monkeypatch.setattr(main_module, "run_in_threadpool", dispatch)
    application = create_app(_demo_settings())
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            ready = await client.get("/health/ready")
            graph = await client.get(
                "/knowledge/search", params={"query": "selective attention"}
            )
            assistant = await client.post(
                "/assistant", json={"question": "How is attention represented?"}
            )
            console = await client.get("/research/console")

    assert ready.status_code == 200
    assert ready.json()["glm_configured"] is False
    assert ready.json()["collection"] == "magicforge_demo_v01"
    assert graph.status_code == 200
    assert graph.json()["nodes"]
    assert graph.json()["relationships"]
    assert assistant.status_code == 200
    assert "generated offline" in assistant.json()["result"]
    assert assistant.json()["answer_format_version"] == "magicforge.reveal.v1"
    assert len(assistant.json()["acts"]) == 3
    assert console.status_code == 200
    assert (
        console.json()["runtime"]["intelligence_instrument"]["provider"]
        == "Offline deterministic Demo"
    )


def test_production_runtime_cannot_fall_back_to_demo() -> None:
    settings = Settings(
        magicforge_profile=MagicForgeProfile.PRODUCTION,
        magicforge_mode=MagicForgeMode.PRODUCTION,
    )

    with pytest.raises(RuntimeInitializationError) as caught:
        build_runtime_services(settings)

    assert caught.value.code == "production_database_unavailable"
