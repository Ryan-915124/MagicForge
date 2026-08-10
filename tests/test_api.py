from dataclasses import replace
from functools import lru_cache

import httpx
import pytest

from analysis.models import AnalysisDimension, MagicTheoryAnalysis
import app.main as main_module
import app.runtime as runtime_module
from app.config import PROJECT_ROOT, Settings
from app.knowledge_read_model import ProjectedKnowledgeReadModel
from app.research_console_read_model import ResearchConsoleReadModel
from app.runtime import RuntimeServices
from app.runtime_corpus import load_active_corpus
from knowledge.governance import MagicForgeMode
from retrieval.interfaces import SearchResult

create_app = main_module.create_app


BOOTSTRAP_ROOT = PROJECT_ROOT / "research/runs/bootstrap-002"
CONSOLE_PROJECT_ROOT = PROJECT_ROOT


@pytest.fixture(autouse=True)
def _use_public_synthetic_corpus(synthetic_bootstrap_corpus):
    global BOOTSTRAP_ROOT, CONSOLE_PROJECT_ROOT

    previous = (BOOTSTRAP_ROOT, CONSOLE_PROJECT_ROOT)
    BOOTSTRAP_ROOT = synthetic_bootstrap_corpus.root
    CONSOLE_PROJECT_ROOT = synthetic_bootstrap_corpus.project_root
    _read_models.cache_clear()
    try:
        yield
    finally:
        _read_models.cache_clear()
        BOOTSTRAP_ROOT, CONSOLE_PROJECT_ROOT = previous


def _bootstrap_settings(**changes) -> Settings:
    settings = Settings(
        glm_api_key="test-key",
        magicforge_mode=MagicForgeMode.BOOTSTRAP,
        bootstrap_allow_anonymous_reads=True,
        qdrant_bootstrap_collection_name="magicforge_bootstrap_v03",
        active_corpus_root=str(BOOTSTRAP_ROOT),
        active_corpus_id="bootstrap-002",
        active_corpus_manifest_path=str(
            BOOTSTRAP_ROOT / "qdrant_manifest/bootstrap-manifest-v03.json"
        ),
        active_corpus_receipt_path=str(
            BOOTSTRAP_ROOT / "qdrant_manifest/ingestion-receipt-v03.json"
        ),
        active_corpus_manifest_schema="bootstrap-manifest-0.2",
        active_qdrant_storage_kind="local",
        active_qdrant_local_path=str(BOOTSTRAP_ROOT / "qdrant_storage_v03"),
    )
    return replace(settings, **changes)


class FakeMagicForgeService:
    def _response(self, value: str):
        return (
            f"Generated for: {value}",
            [
                SearchResult(
                    text="Context",
                    score=0.95,
                    payload={
                        "title": "Theory Notes",
                        "author": "Expert",
                        "document_id": "document-1",
                        "entity_ids": ["entity-1"],
                        "artifact_type": "evidence_card",
                        "knowledge_type": "evidence",
                        "knowledge_origin": "scientific_evidence",
                        "evidence_level": "empirical",
                        "evidence_class": "controlled_experiment",
                        "confidence": 0.82,
                        "confidence_label": "high",
                        "limitations": ["Laboratory setting."],
                        "contradiction_status": "none_found",
                        "evidence_card_id": "evidence-card-1",
                    },
                )
            ],
        )

    def assistant(self, value: str, *, authorization):
        assert authorization.bootstrap_limited is True
        return self._response(value)

    def analyze(self, value: str, *, authorization):
        assert authorization.bootstrap_limited is True
        _, sources = self._response(value)
        dimension = AnalysisDimension(
            score=4.0,
            summary="Strong",
            criteria=[],
            risks=[],
            recommendations=[],
        )
        return (
            MagicTheoryAnalysis(
                overall_score=4.0,
                effect_strength=dimension,
                method_concealment=dimension,
                psychological_principles=dimension,
                audience_experience=dimension,
                performance_design=dimension,
                assumptions=[],
                overall_assessment="Strong overall",
            ),
            sources,
        )

    def create(self, value: str, *, authorization):
        assert authorization.bootstrap_limited is True
        return self._response(value)


class StructuredMagicForgeService(FakeMagicForgeService):
    answer = """A short lead.
[[MAGICFORGE_ACT:EFFECT]]
The audience sees an impossible restoration.
[[MAGICFORGE_ACT:COGNITIVE_MECHANISM]]
Expectation amplifies the violation.
[[MAGICFORGE_SYNTHESIS]]
Separate the remembered effect from its construction."""

    def assistant(self, _value: str, *, authorization):
        assert authorization.bootstrap_limited is True
        _, sources = self._response(_value)
        return self.answer, sources

    def create(self, _value: str, *, authorization):
        assert authorization.bootstrap_limited is True
        _, sources = self._response(_value)
        return self.answer, sources


class FakeRetriever:
    def __init__(self, corpus) -> None:
        self.collection_name = corpus.collection_name
        self.active_corpus_id = corpus.corpus_id
        self.active_manifest_id = corpus.manifest_id
        self.active_manifest_hash = corpus.manifest_hash
        self.active_projection_schema = corpus.projection_schema_version
        self.active_payload_checksums = dict(corpus.expected_payload_checksums)
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


@lru_cache(maxsize=1)
def _read_models():
    active_corpus = load_active_corpus(_bootstrap_settings())
    knowledge_read_model = ProjectedKnowledgeReadModel(active_corpus)
    knowledge_read_model.validate()
    research_console_read_model = ResearchConsoleReadModel(
        active_corpus, CONSOLE_PROJECT_ROOT
    )
    return active_corpus, knowledge_read_model, research_console_read_model


def _runtime_services(service: FakeMagicForgeService) -> RuntimeServices:
    active_corpus, knowledge_read_model, research_console_read_model = (
        _read_models()
    )
    service.active_corpus = active_corpus
    return RuntimeServices(
        active_corpus=active_corpus,
        magicforge_service=service,
        knowledge_read_model=knowledge_read_model,
        research_console_read_model=research_console_read_model,
        retriever=FakeRetriever(active_corpus),
    )


def _app(
    service: FakeMagicForgeService | None = None,
    settings: Settings | None = None,
):
    selected_service = service or FakeMagicForgeService()
    return create_app(
        settings=settings or _bootstrap_settings(),
        runtime_builder=lambda _settings: _runtime_services(selected_service),
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def deterministic_threadpool_dispatch(monkeypatch):
    """Keep endpoint tests deterministic while preserving the dispatch seam."""

    async def dispatch(operation, *args):
        return operation(*args)

    monkeypatch.setattr(main_module, "run_in_threadpool", dispatch)


async def _request(method: str, path: str, **kwargs):
    application = _app()
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)


@pytest.mark.anyio
async def test_health_reports_configuration(synthetic_bootstrap_corpus) -> None:
    response = await _request("GET", "/health")
    assert response.status_code == 200
    body = response.json()
    assert body["glm_configured"] is True
    assert body["mode"] == "bootstrap"
    assert body["corpus_id"] == "bootstrap-002"
    assert body["collection"] == "magicforge_bootstrap_v03"
    assert body["schema_version"] == "bootstrap-qdrant-0.2"
    assert body["manifest_schema_version"] == "bootstrap-manifest-0.2"
    assert body["manifest_id"] == synthetic_bootstrap_corpus.manifest_id
    assert body["qdrant_url"] == "local"


@pytest.mark.anyio
async def test_stats_exposes_audited_projection_distributions(
    synthetic_bootstrap_corpus,
) -> None:
    response = await _request("GET", "/stats")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == "bootstrap-002"
    assert body["sources"] == synthetic_bootstrap_corpus.source_count
    assert (
        body["sources_with_projected_knowledge"]
        == synthetic_bootstrap_corpus.source_count
    )
    assert body["source_categories"] == synthetic_bootstrap_corpus.source_categories
    assert body["distributions"]["scope"] == "projected_points"
    assert (
        body["distributions"]["domain_memberships"]
        == synthetic_bootstrap_corpus.domain_memberships
    )
    assert (
        body["distributions"]["knowledge_origins"]
        == synthetic_bootstrap_corpus.knowledge_origins
    )
    assert body["governance"] == {
        "pending_human_review_sources": synthetic_bootstrap_corpus.source_count,
        "contradiction_checks_pending": (
            synthetic_bootstrap_corpus.evidence_card_count
        ),
        "procedural_method_projections_quarantined": 0,
        "production_collection_touched": False,
    }


@pytest.mark.anyio
async def test_research_console_separates_configuration_from_audited_state(
    synthetic_bootstrap_corpus,
) -> None:
    secret = "glm-secret-must-not-be-returned"
    private_storage_path = str(BOOTSTRAP_ROOT / "qdrant_storage_v03")
    application = _app(
        settings=_bootstrap_settings(
            glm_api_key=secret,
            glm_model="glm-4.7",
        )
    )

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/research/console")

    assert response.status_code == 200
    body = response.json()
    assert body["current_run"] == {
        "run_id": "bootstrap-002",
        "mode": "bootstrap",
        "generated_at": "2026-01-01T00:00:00Z",
        "collection": "magicforge_bootstrap_v03",
        "status": "receipt_verified",
    }
    assert body["runtime"]["api"] == {"status": "ok"}
    assert body["runtime"]["intelligence_instrument"] == {
        "provider": "Zhipu GLM",
        "model": "glm-4.7",
        "configured": True,
        "connectivity": "not_probed",
        "structured_extraction": True,
    }
    assert body["runtime"]["retrieval"] == {
        "configured": True,
        "collection": "magicforge_bootstrap_v03",
        "storage_kind": "local",
        "connectivity": "not_probed",
    }
    assert body["memory_vault"]["alignment_status"] == "aligned"
    assert body["memory_vault"]["manifest"]["status"] == "manifest_verified"
    assert body["memory_vault"]["receipt"]["status"] == "receipt_verified"
    assert body["memory_vault"]["points"] == {
        "manifest": synthetic_bootstrap_corpus.point_count,
        "receipt": synthetic_bootstrap_corpus.point_count,
        "smoke_observed": synthetic_bootstrap_corpus.point_count,
    }
    assert body["memory_vault"]["retrieval_smoke"] == {
        "status": "report_verified",
        "tested_at": "2026-01-01T00:02:00Z",
        "query_count": 2,
        "collection_count": synthetic_bootstrap_corpus.point_count,
        "all_returned_hits_bootstrap_safe": True,
    }
    assert body["governance"]["checkpoint_status"] == "pending_human_review"
    assert body["governance"]["human_verified_points"] == 0
    assert [item["run_id"] for item in body["run_history"]] == [
        "bootstrap-001",
        "bootstrap-001-v02",
        "bootstrap-002",
    ]
    assert [item["qdrant_points"] for item in body["run_history"]] == [
        7,
        11,
        synthetic_bootstrap_corpus.point_count,
    ]
    assert [item["metric_basis"] for item in body["run_history"]] == [
        "reported_generated_outputs",
        "reported_generated_outputs",
        "receipt_verified_projections",
    ]
    assert body["pipeline"]["stages"][-1] == {
        "id": "memory_projection",
        "label": "Memory projection",
        "status": "receipt_verified",
        "metrics": {"qdrant_points": synthetic_bootstrap_corpus.point_count},
    }

    serialized = response.text
    assert secret not in serialized
    assert private_storage_path not in serialized


@pytest.mark.anyio
async def test_generation_endpoints_use_expected_request_fields() -> None:
    generation_cases = [
        ("/assistant", {"question": "Why does this work?"}),
        ("/create", {"requirements": "Create a parlor effect."}),
    ]
    for path, payload in generation_cases:
        response = await _request("POST", path, json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["result"].startswith("Generated for:")
        assert body["sources"][0]["entity_ids"] == ["entity-1"]
        assert body["sources"][0]["knowledge_origin"] == "scientific_evidence"
        assert body["sources"][0]["limitations"] == ["Laboratory setting."]
        assert body["answer_format_version"] is None
        assert body["lead"] is None
        assert body["acts"] is None
        assert body["synthesis"] is None

    response = await _request(
        "POST", "/analyze", json={"magic_description": "A card rises."}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis"]["schema_version"] == "0.2"
    assert body["analysis"]["effect_strength"]["score"] == 4.0
    assert body["sources"][0]["entity_ids"] == ["entity-1"]


@pytest.mark.anyio
async def test_assistant_projects_optional_reveal_without_replacing_raw_result() -> None:
    application = _app(service=StructuredMagicForgeService())

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/assistant", json={"question": "Explain it."}
            )

    assert response.status_code == 200
    body = response.json()
    assert body["result"] == StructuredMagicForgeService.answer
    assert body["answer_format_version"] == "magicforge.reveal.v1"
    assert body["lead"] == "A short lead."
    assert body["acts"] == [
        {
            "kind": "effect",
            "content": "The audience sees an impossible restoration.",
        },
        {
            "kind": "cognitive_mechanism",
            "content": "Expectation amplifies the violation.",
        },
    ]
    assert body["synthesis"] == (
        "Separate the remembered effect from its construction."
    )


@pytest.mark.anyio
async def test_create_never_projects_the_assistant_reveal_contract() -> None:
    application = _app(service=StructuredMagicForgeService())

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/create", json={"requirements": "Create an effect."}
            )

    assert response.status_code == 200
    body = response.json()
    assert body["result"] == StructuredMagicForgeService.answer
    assert body["answer_format_version"] is None
    assert body["lead"] is None
    assert body["acts"] is None
    assert body["synthesis"] is None


@pytest.mark.anyio
async def test_malformed_assistant_answer_uses_legacy_response_fields() -> None:
    service = StructuredMagicForgeService()
    service.answer = """[[MAGICFORGE_ACT:EFFECT]]
Effect.
[[MAGICFORGE_ACT:EFFECT]]
Duplicate marker."""
    application = _app(service=service)

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/assistant", json={"question": "Explain it."}
            )

    assert response.status_code == 200
    body = response.json()
    assert body["result"] == service.answer
    assert body["answer_format_version"] is None
    assert body["acts"] is None


@pytest.mark.anyio
async def test_empty_request_is_rejected() -> None:
    response = await _request("POST", "/assistant", json={"question": ""})
    assert response.status_code == 422


@pytest.mark.anyio
async def test_missing_knowledge_resources_remain_not_found() -> None:
    node_response = await _request("GET", "/knowledge/node/not-a-node")
    evidence_response = await _request("GET", "/evidence/not-an-evidence-card")

    assert node_response.status_code == 404
    assert node_response.json()["detail"] == "knowledge node not found"
    assert evidence_response.status_code == 404
    assert evidence_response.json()["detail"] == "Evidence Card not found"


@pytest.mark.anyio
async def test_generation_operations_use_threadpool_dispatch(monkeypatch) -> None:
    dispatched_operations: list[tuple[str, str]] = []

    async def recording_dispatch(operation, *args):
        dispatched_operations.append((operation.__name__, args[0].__name__))
        return operation(*args)

    monkeypatch.setattr(main_module, "run_in_threadpool", recording_dispatch)
    service = FakeMagicForgeService()
    application = _app(service=service)

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            responses = [
                await client.post("/assistant", json={"question": "Why?"}),
                await client.post(
                    "/analyze", json={"magic_description": "A vanish."}
                ),
                await client.post("/create", json={"requirements": "A reveal."}),
            ]

    assert all(response.status_code == 200 for response in responses)
    assert dispatched_operations == [
        ("_invoke", "assistant"),
        ("_invoke", "analyze"),
        ("_invoke", "create"),
    ]


@pytest.mark.anyio
async def test_lifespan_passes_glm_budget_to_client(monkeypatch) -> None:
    constructed = {}

    class RecordingGLM:
        model = "glm-4.7"

        def __init__(self, **kwargs) -> None:
            constructed.update(kwargs)

    monkeypatch.setattr(runtime_module, "GLMClient", RecordingGLM)

    def runtime_builder(settings: Settings) -> RuntimeServices:
        service = FakeMagicForgeService()
        service.llm = runtime_module._default_glm_factory(settings)
        return _runtime_services(service)

    application = create_app(
        settings=_bootstrap_settings(
            glm_timeout_seconds=72.0,
            glm_max_retries=1,
        ),
        runtime_builder=runtime_builder,
    )

    assert constructed == {}
    async with application.router.lifespan_context(application):
        assert constructed == {
            "api_key": "test-key",
            "model": "glm-4.7",
            "timeout_seconds": 72.0,
            "max_retries": 1,
        }

    assert constructed == {
        "api_key": "test-key",
        "model": "glm-4.7",
        "timeout_seconds": 72.0,
        "max_retries": 1,
    }
