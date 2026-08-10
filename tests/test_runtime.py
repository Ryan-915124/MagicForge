from __future__ import annotations

import subprocess
import sys
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from qdrant_client import models

from app.config import PROJECT_ROOT, Settings
from app.main import create_app
from app.runtime import (
    RuntimeInitializationError,
    RuntimePhase,
    RuntimeServices,
    build_runtime_services,
)
from app.runtime_corpus import ActiveCorpus, load_active_corpus
from knowledge.governance import MagicForgeMode
from knowledge.bootstrap import BootstrapQdrantProjection
from pydantic import TypeAdapter
from tests.support.synthetic_bootstrap import (
    SyntheticBootstrapCorpus,
    bootstrap_settings as make_bootstrap_settings,
)


def _bootstrap_settings(
    fixture: SyntheticBootstrapCorpus,
    *,
    glm_api_key: str = "test-key",
) -> Settings:
    return make_bootstrap_settings(fixture, glm_api_key=glm_api_key)


class _FakeRetriever:
    def __init__(self, corpus: ActiveCorpus) -> None:
        self.collection_name = corpus.collection_name
        self.active_corpus_id = corpus.corpus_id
        self.active_manifest_id = corpus.manifest_id
        self.active_manifest_hash = corpus.manifest_hash
        self.active_projection_schema = corpus.projection_schema_version
        self.active_payload_checksums = dict(corpus.expected_payload_checksums)
        self.close_calls = 0
        self.records: list[SimpleNamespace] = []

    def close(self) -> None:
        self.close_calls += 1


class _ReadyDatabaseManager:
    def start(self) -> None:
        return None

    def require_ready(self):
        return object()

    def close(self) -> None:
        return None


def _ready_database_factory(_url: str, _timeout: float):
    return _ReadyDatabaseManager()


def _fake_runtime(corpus: ActiveCorpus) -> RuntimeServices:
    retriever = _FakeRetriever(corpus)
    return RuntimeServices(
        active_corpus=corpus,
        magicforge_service=SimpleNamespace(active_corpus=corpus),
        knowledge_read_model=SimpleNamespace(active_corpus=corpus),
        research_console_read_model=SimpleNamespace(active_corpus=corpus),
        retriever=retriever,
    )


class _FakeEmbedding:
    dimension = 384

    def embed_documents(self, texts):
        return [[0.0] * self.dimension for _ in texts]

    def embed_query(self, _text: str):
        return [0.0] * self.dimension


class _FakeQdrantClient:
    def __init__(self, *, collection_exists: bool, point_count: int) -> None:
        self._collection_exists = collection_exists
        self._point_count = point_count
        self.collection_exists_calls: list[str] = []
        self.count_calls: list[dict[str, Any]] = []
        self.create_collection_calls = 0
        self.close_calls = 0

    def collection_exists(self, collection_name: str) -> bool:
        self.collection_exists_calls.append(collection_name)
        return self._collection_exists

    def get_collection(self, _collection_name: str):
        vectors = SimpleNamespace(size=384, distance=models.Distance.COSINE)
        return SimpleNamespace(
            config=SimpleNamespace(params=SimpleNamespace(vectors=vectors))
        )

    def count(self, **kwargs):
        self.count_calls.append(kwargs)
        return SimpleNamespace(count=self._point_count)

    def retrieve(self, *, ids, **_kwargs):
        wanted = {str(value) for value in ids}
        return [record for record in self.records if str(record.id) in wanted]

    def create_collection(self, **_kwargs) -> None:
        self.create_collection_calls += 1
        raise AssertionError("readiness must never create a collection")

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture(scope="module")
def bootstrap_settings(synthetic_bootstrap_corpus) -> Settings:
    return _bootstrap_settings(synthetic_bootstrap_corpus)


@pytest.fixture(scope="module")
def active_corpus(bootstrap_settings: Settings) -> ActiveCorpus:
    return load_active_corpus(bootstrap_settings)


def _authoritative_bootstrap_records(settings: Settings) -> list[SimpleNamespace]:
    raw = json.loads(Path(settings.active_corpus_manifest_path).read_text(encoding="utf-8"))
    projections = TypeAdapter(list[BootstrapQdrantProjection]).validate_python(
        raw["projections"]
    )
    return [
        SimpleNamespace(
            id=projection.knowledge_unit_id,
            payload=projection.to_payload(
                manifest_id=raw["id"],
                manifest_hash=raw["manifest_hash"],
            ),
        )
        for projection in projections
    ]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_import_and_create_app_do_not_open_qdrant(
    synthetic_bootstrap_corpus,
) -> None:
    root = Path(PROJECT_ROOT)
    corpus_root = synthetic_bootstrap_corpus.root
    script = f"""
import qdrant_client

class ForbiddenClient:
    def __init__(self, *args, **kwargs):
        raise AssertionError('Qdrant opened during import or create_app')

qdrant_client.QdrantClient = ForbiddenClient
import app.main
from app.config import Settings
from knowledge.governance import MagicForgeMode

root = {str(corpus_root)!r}
settings = Settings(
    magicforge_mode=MagicForgeMode.BOOTSTRAP,
    active_corpus_root=root,
    active_corpus_id='bootstrap-002',
    active_corpus_manifest_path=root + '/qdrant_manifest/bootstrap-manifest-v03.json',
    active_corpus_receipt_path=root + '/qdrant_manifest/ingestion-receipt-v03.json',
    active_corpus_manifest_schema='bootstrap-manifest-0.2',
    active_qdrant_storage_kind='local',
    active_qdrant_local_path=root + '/qdrant_storage_v03',
)
application = app.main.create_app(settings=settings)
assert application.state.runtime_container.phase.value == 'not_started'
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.anyio
async def test_lifespan_starts_and_closes_injected_runtime_exactly_once(
    bootstrap_settings: Settings,
    active_corpus: ActiveCorpus,
) -> None:
    runtime = _fake_runtime(active_corpus)
    builder_calls: list[Settings] = []

    def builder(settings: Settings) -> RuntimeServices:
        builder_calls.append(settings)
        return runtime

    application = create_app(
        settings=bootstrap_settings,
        runtime_builder=builder,
    )
    container = application.state.runtime_container

    assert builder_calls == []
    assert container.phase == RuntimePhase.NOT_STARTED
    async with application.router.lifespan_context(application):
        assert builder_calls == [bootstrap_settings]
        assert container.phase == RuntimePhase.READY
        assert container.services is runtime
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["corpus_id"] == "bootstrap-002"

    assert container.phase == RuntimePhase.CLOSED
    assert runtime.retriever.close_calls == 1
    container.close()
    assert runtime.retriever.close_calls == 1


@pytest.mark.anyio
async def test_failed_initialization_keeps_live_endpoint_and_returns_safe_503(
    bootstrap_settings: Settings,
    active_corpus: ActiveCorpus,
) -> None:
    secret = "glm-secret-that-must-not-leak"
    private_path = str(active_corpus.local_storage_path)
    settings = replace(bootstrap_settings, glm_api_key=secret)

    def failing_builder(_settings: Settings) -> RuntimeServices:
        raise RuntimeError(f"dependency failure: {secret} at {private_path}")

    application = create_app(settings=settings, runtime_builder=failing_builder)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")
            health = await client.get("/health")

    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    for response in (ready, health):
        assert response.status_code == 503
        assert response.json() == {
            "detail": {
                "code": "runtime_initialization_failed",
                "message": "Runtime initialization failed. Check server diagnostics.",
            }
        }
        assert secret not in response.text
        assert private_path not in response.text


@pytest.mark.anyio
async def test_injected_runtime_exposes_exact_active_corpus_identity(
    bootstrap_settings: Settings,
    active_corpus: ActiveCorpus,
) -> None:
    runtime = _fake_runtime(active_corpus)
    application = create_app(
        settings=bootstrap_settings,
        runtime_builder=lambda _settings: runtime,
    )

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/health/ready")

        assert application.state.runtime_container.services is runtime
        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "mode": "bootstrap",
            "profile": "development",
            "read_only": False,
            "synthetic_corpus": False,
            "corpus_id": active_corpus.corpus_id,
            "schema_version": active_corpus.projection_schema_version,
            "manifest_schema_version": active_corpus.manifest_schema_version,
            "manifest_id": active_corpus.manifest_id,
            "collection": active_corpus.collection_name,
            "storage_kind": active_corpus.storage_kind,
            "glm_configured": True,
            "glm_connectivity": "not_probed",
        }


def test_runtime_services_require_one_active_corpus_object(
    active_corpus: ActiveCorpus,
) -> None:
    runtime = _fake_runtime(active_corpus)

    assert runtime.magicforge_service.active_corpus is active_corpus
    assert runtime.knowledge_read_model.active_corpus is active_corpus
    assert runtime.research_console_read_model.active_corpus is active_corpus

    equivalent_but_distinct = replace(active_corpus)
    with pytest.raises(RuntimeInitializationError) as exc_info:
        RuntimeServices(
            active_corpus=active_corpus,
            magicforge_service=SimpleNamespace(
                active_corpus=equivalent_but_distinct
            ),
            knowledge_read_model=SimpleNamespace(active_corpus=active_corpus),
            research_console_read_model=SimpleNamespace(
                active_corpus=active_corpus
            ),
            retriever=_FakeRetriever(active_corpus),
        )

    assert exc_info.value.code == "runtime_corpus_identity_mismatch"


def test_runtime_services_reject_retriever_manifest_identity_mismatch(
    active_corpus: ActiveCorpus,
) -> None:
    retriever = _FakeRetriever(active_corpus)
    retriever.active_manifest_hash = "0" * 64

    with pytest.raises(RuntimeInitializationError) as exc_info:
        RuntimeServices(
            active_corpus=active_corpus,
            magicforge_service=SimpleNamespace(active_corpus=active_corpus),
            knowledge_read_model=SimpleNamespace(active_corpus=active_corpus),
            research_console_read_model=SimpleNamespace(
                active_corpus=active_corpus
            ),
            retriever=retriever,
        )

    assert exc_info.value.code == "runtime_retrieval_identity_mismatch"


def test_runtime_factory_shares_active_corpus_across_real_adapters(
    bootstrap_settings: Settings,
    synthetic_bootstrap_corpus,
) -> None:
    client = _FakeQdrantClient(
        collection_exists=True,
        point_count=synthetic_bootstrap_corpus.point_count,
    )
    client.records = _authoritative_bootstrap_records(bootstrap_settings)
    factory_corpora: list[ActiveCorpus] = []

    def client_factory(corpus: ActiveCorpus) -> _FakeQdrantClient:
        factory_corpora.append(corpus)
        return client

    runtime = build_runtime_services(
        bootstrap_settings,
        qdrant_client_factory=client_factory,
        embedding_factory=lambda _settings: _FakeEmbedding(),
        glm_factory=lambda _settings: SimpleNamespace(model="glm-4.7"),
    )

    assert factory_corpora == [runtime.active_corpus]
    assert factory_corpora[0] is runtime.active_corpus
    assert runtime.magicforge_service.active_corpus is runtime.active_corpus
    assert runtime.knowledge_read_model.active_corpus is runtime.active_corpus
    assert runtime.research_console_read_model.active_corpus is runtime.active_corpus
    assert runtime.retriever.collection_name == runtime.active_corpus.collection_name
    assert len(client.count_calls) == 2
    assert client.create_collection_calls == 0

    runtime.close()
    runtime.close()
    assert client.close_calls == 1


@pytest.mark.anyio
async def test_missing_qdrant_collection_fails_closed_without_creating_it(
    bootstrap_settings: Settings,
) -> None:
    client = _FakeQdrantClient(collection_exists=False, point_count=0)
    glm_factory_calls = 0

    def glm_factory(_settings: Settings):
        nonlocal glm_factory_calls
        glm_factory_calls += 1
        return SimpleNamespace(model="glm-4.7")

    def builder(settings: Settings) -> RuntimeServices:
        return build_runtime_services(
            settings,
            qdrant_client_factory=lambda _corpus: client,
            embedding_factory=lambda _settings: _FakeEmbedding(),
            glm_factory=glm_factory,
        )

    application = create_app(
        settings=bootstrap_settings,
        runtime_builder=builder,
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as http_client:
            live = await http_client.get("/health/live")
            ready = await http_client.get("/health/ready")

    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json() == {
        "detail": {
            "code": "qdrant_not_ready",
            "message": (
                "Embedded Qdrant is unavailable. Use one process or configure "
                "Qdrant server mode."
            ),
        }
    }
    assert client.collection_exists_calls == ["magicforge_bootstrap_v03"]
    assert client.create_collection_calls == 0
    assert client.close_calls == 1
    assert glm_factory_calls == 0


@pytest.mark.anyio
async def test_production_endpoints_reject_injected_bootstrap_runtime(
    active_corpus: ActiveCorpus,
) -> None:
    runtime = _fake_runtime(active_corpus)
    application = create_app(
        settings=Settings(
            magicforge_mode=MagicForgeMode.PRODUCTION,
            database_url="postgresql+psycopg://test:test@db/magicforge",
        ),
        runtime_builder=lambda _settings: runtime,
        database_manager_factory=_ready_database_factory,
    )

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            ready = await client.get("/health/ready")
            stats = await client.get("/stats")

    assert ready.status_code == 503
    assert ready.json() == {
        "detail": {
            "code": "runtime_mode_mismatch",
            "message": "Runtime services do not match the configured mode.",
        }
    }
    assert stats.status_code == 401
    assert stats.json()["detail"]["code"] == "unauthenticated"
    assert runtime.retriever.close_calls == 1


@pytest.mark.anyio
async def test_production_without_authorized_corpus_is_not_ready() -> None:
    application = create_app(
        settings=Settings(
            magicforge_mode=MagicForgeMode.PRODUCTION,
            database_url="postgresql+psycopg://test:test@db/magicforge",
        ),
        database_manager_factory=_ready_database_factory,
    )

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")

    assert live.status_code == 200
    assert ready.status_code == 503
    assert ready.json() == {
        "detail": {
            "code": "active_corpus_not_configured",
            "message": (
                "Active corpus identity, manifest, receipt, and schema must "
                "be explicit."
            ),
        }
    }
