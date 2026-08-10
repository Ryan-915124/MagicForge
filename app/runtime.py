"""Mode-safe runtime composition and lifecycle ownership."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import qdrant_client

from app.config import MagicForgeProfile, Settings
from app.demo_read_model import (
    DemoKnowledgeReadModel,
    DemoResearchConsoleReadModel,
)
from app.knowledge_read_model import ProjectedKnowledgeReadModel
from app.production_read_model import (
    ProductionKnowledgeReadModel,
    load_active_production_corpus,
)
from app.production_research_console import ProductionResearchConsoleReadModel
from app.research_console_read_model import ResearchConsoleReadModel
from app.runtime_corpus import (
    ActiveCorpus,
    ActiveCorpusConfigurationError,
    load_active_corpus,
)
from app.service import MagicForgeService
from knowledge.governance import MagicForgeMode
from knowledge.demo import load_demo_bundle
from llm.demo_client import DemoLanguageModel
from llm.glm_client import GLMClient
from retrieval.demo import (
    DeterministicDemoEmbeddingProvider,
    ReadOnlyDemoQdrantService,
    seed_demo_collection,
)
from retrieval.embeddings import EmbeddingProvider, FastEmbedProvider
from retrieval.qdrant_service import QdrantService, QdrantServiceError


logger = logging.getLogger(__name__)


class RuntimePhase(StrEnum):
    NOT_STARTED = "not_started"
    READY = "ready"
    FAILED = "failed"
    CLOSED = "closed"


class RuntimeInitializationError(RuntimeError):
    """A startup failure with a stable, public-safe representation."""

    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


@dataclass(frozen=True, slots=True)
class RuntimeFailure:
    code: str
    message: str


@dataclass(slots=True)
class RuntimeServices:
    """Dependencies built from exactly one immutable active corpus."""

    active_corpus: ActiveCorpus
    magicforge_service: MagicForgeService
    knowledge_read_model: (
        DemoKnowledgeReadModel
        | ProjectedKnowledgeReadModel
        | ProductionKnowledgeReadModel
    )
    research_console_read_model: (
        DemoResearchConsoleReadModel
        | ResearchConsoleReadModel
        | ProductionResearchConsoleReadModel
    )
    retriever: QdrantService
    freshness_guard: Callable[[], None] | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        identities = (
            self.magicforge_service.active_corpus,
            self.knowledge_read_model.active_corpus,
            self.research_console_read_model.active_corpus,
        )
        if any(item is not self.active_corpus for item in identities):
            raise RuntimeInitializationError(
                "runtime_corpus_identity_mismatch",
                "Runtime services were not built from one active corpus identity.",
            )
        if self.retriever.collection_name != self.active_corpus.collection_name:
            raise RuntimeInitializationError(
                "runtime_collection_mismatch",
                "Runtime retrieval does not target the active corpus collection.",
            )
        retriever_identity = (
            self.retriever.active_corpus_id,
            self.retriever.active_manifest_id,
            self.retriever.active_manifest_hash,
            self.retriever.active_projection_schema,
            self.retriever.active_payload_checksums,
        )
        expected_identity = (
            self.active_corpus.corpus_id,
            self.active_corpus.manifest_id,
            self.active_corpus.manifest_hash,
            self.active_corpus.projection_schema_version,
            dict(self.active_corpus.expected_payload_checksums),
        )
        if retriever_identity != expected_identity:
            raise RuntimeInitializationError(
                "runtime_retrieval_identity_mismatch",
                "Runtime retrieval does not match the active corpus manifest.",
            )

    def validate_current(self) -> None:
        if self._closed:
            raise RuntimeInitializationError(
                "runtime_closed", "Runtime services are closed."
            )
        if self.freshness_guard is None:
            return
        try:
            self.freshness_guard()
        except RuntimeInitializationError:
            raise
        except Exception as exc:
            raise RuntimeInitializationError(
                "active_corpus_stale",
                "The active Production corpus changed or lost approval; restart is required.",
            ) from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.retriever.close()


RuntimeBuilder = Callable[[Settings], RuntimeServices]
QdrantClientFactory = Callable[[ActiveCorpus], Any]
EmbeddingFactory = Callable[[Settings], EmbeddingProvider]
GLMFactory = Callable[[Settings], GLMClient]


class RuntimeContainer:
    """Start and close one process-local runtime exactly once."""

    def __init__(self, settings: Settings, builder: RuntimeBuilder) -> None:
        self.settings = settings
        self.builder = builder
        self.phase = RuntimePhase.NOT_STARTED
        self.services: RuntimeServices | None = None
        self.failure: RuntimeFailure | None = None

    def start(self) -> None:
        if self.phase != RuntimePhase.NOT_STARTED:
            raise RuntimeError("runtime may only be started once")
        try:
            services = self.builder(self.settings)
            if services.active_corpus.mode != self.settings.magicforge_mode:
                services.close()
                raise RuntimeInitializationError(
                    "runtime_mode_mismatch",
                    "Runtime services do not match the configured mode.",
                )
            if (
                self.settings.magicforge_mode == MagicForgeMode.PRODUCTION
                and services.active_corpus.bootstrap_generated
            ):
                services.close()
                raise RuntimeInitializationError(
                    "runtime_mode_mismatch",
                    "Production mode cannot expose Bootstrap runtime services.",
                )
            self.services = services
        except ActiveCorpusConfigurationError as exc:
            self.phase = RuntimePhase.FAILED
            self.failure = RuntimeFailure(exc.code, exc.public_message)
        except RuntimeInitializationError as exc:
            self.phase = RuntimePhase.FAILED
            self.failure = RuntimeFailure(exc.code, exc.public_message)
        except Exception:
            logger.exception("unexpected MagicForge runtime initialization failure")
            self.phase = RuntimePhase.FAILED
            self.failure = RuntimeFailure(
                "runtime_initialization_failed",
                "Runtime initialization failed. Check server diagnostics.",
            )
        else:
            self.phase = RuntimePhase.READY

    def require_ready(self) -> RuntimeServices:
        if self.phase == RuntimePhase.READY and self.services is not None:
            self.services.validate_current()
            return self.services
        failure = self.failure or RuntimeFailure(
            "runtime_not_started",
            "Runtime initialization has not completed.",
        )
        raise RuntimeInitializationError(failure.code, failure.message)

    def close(self) -> None:
        if self.phase == RuntimePhase.CLOSED:
            return
        if self.services is not None:
            try:
                self.services.close()
            except Exception:
                logger.exception("MagicForge runtime shutdown failed")
        self.phase = RuntimePhase.CLOSED


def build_runtime_services(
    settings: Settings,
    *,
    qdrant_client_factory: QdrantClientFactory | None = None,
    embedding_factory: EmbeddingFactory | None = None,
    glm_factory: GLMFactory | None = None,
    session_factory=None,
) -> RuntimeServices:
    """Build one read runtime without creating or modifying corpus data."""

    profile = settings.effective_profile
    demo_bundle = None
    if profile == MagicForgeProfile.DEMO:
        active_corpus = load_active_corpus(settings)
        if active_corpus.manifest_path is None:
            raise RuntimeInitializationError(
                "demo_corpus_invalid",
                "The committed Demo corpus is missing or invalid.",
            )
        demo_bundle = load_demo_bundle(active_corpus.manifest_path)
        knowledge_read_model = DemoKnowledgeReadModel(active_corpus, demo_bundle)
        research_console_read_model = DemoResearchConsoleReadModel(
            active_corpus, demo_bundle
        )
        freshness_guard = None
    elif settings.magicforge_mode == MagicForgeMode.PRODUCTION:
        if session_factory is None:
            raise RuntimeInitializationError(
                "production_database_unavailable",
                "Production knowledge requires a ready governance database.",
            )
        active_corpus = load_active_production_corpus(settings, session_factory)
        knowledge_read_model = ProductionKnowledgeReadModel(
            active_corpus, session_factory
        )
        research_console_read_model = ProductionResearchConsoleReadModel(
            active_corpus
        )

        def validate_production_freshness() -> None:
            try:
                knowledge_read_model.validate()
            except Exception as exc:
                raise RuntimeInitializationError(
                    "active_corpus_stale",
                    "The active Production corpus changed or lost approval; restart is required.",
                ) from exc

        freshness_guard: Callable[[], None] | None = validate_production_freshness
    else:
        active_corpus = load_active_corpus(settings)
        knowledge_read_model = ProjectedKnowledgeReadModel(active_corpus)
        research_console_read_model = ResearchConsoleReadModel(active_corpus)
        freshness_guard = None
    try:
        knowledge_read_model.validate()
    except Exception as exc:
        raise RuntimeInitializationError(
            "knowledge_artifacts_invalid",
            "Active corpus read artifacts are missing or inconsistent.",
        ) from exc

    embeddings: EmbeddingProvider
    if profile == MagicForgeProfile.DEMO:
        # Never instantiate FastEmbed or any caller-provided model in Demo.
        embeddings = DeterministicDemoEmbeddingProvider(
            settings.embedding_dimension
        )
    else:
        embeddings = (embedding_factory or _default_embedding_factory)(settings)
    client: Any | None = None
    retriever: QdrantService | None = None
    try:
        if profile == MagicForgeProfile.DEMO:
            client = (
                qdrant_client_factory(active_corpus)
                if qdrant_client_factory is not None
                else qdrant_client.QdrantClient(location=":memory:")
            )
            assert demo_bundle is not None
            assert isinstance(embeddings, DeterministicDemoEmbeddingProvider)
            seed_demo_collection(client, demo_bundle, embeddings)
        else:
            client = (qdrant_client_factory or _default_qdrant_client_factory)(
                active_corpus
            )
        retriever_type = (
            ReadOnlyDemoQdrantService
            if profile == MagicForgeProfile.DEMO
            else QdrantService
        )
        retriever = retriever_type(
            url=active_corpus.server_url or "embedded",
            collection_name=active_corpus.collection_name,
            embedding_provider=embeddings,
            client=client,
            mode=active_corpus.mode,
            active_corpus_id=active_corpus.corpus_id,
            active_manifest_id=active_corpus.manifest_id,
            active_manifest_hash=active_corpus.manifest_hash,
            active_projection_schema=active_corpus.projection_schema_version,
            active_payload_checksums=dict(
                active_corpus.expected_payload_checksums
            ),
        )
        retriever.validate_readiness(
            expected_point_count=active_corpus.expected_point_count,
            expected_point_ids=active_corpus.expected_point_ids,
            expected_payload_checksums=dict(
                active_corpus.expected_payload_checksums
            ),
            corpus_id=active_corpus.corpus_id,
            manifest_id=active_corpus.manifest_id,
            manifest_hash=active_corpus.manifest_hash,
            expected_projection_schema=active_corpus.projection_schema_version,
        )
        llm = (
            DemoLanguageModel()
            if profile == MagicForgeProfile.DEMO
            else (glm_factory or _default_glm_factory)(settings)
        )
        magicforge_service = MagicForgeService(
            llm=llm,
            retriever=retriever,
            retrieval_limit=settings.retrieval_limit,
            active_corpus=active_corpus,
            runtime_preflight=freshness_guard,
        )
        return RuntimeServices(
            active_corpus=active_corpus,
            magicforge_service=magicforge_service,
            knowledge_read_model=knowledge_read_model,
            research_console_read_model=research_console_read_model,
            retriever=retriever,
            freshness_guard=freshness_guard,
        )
    except RuntimeInitializationError:
        _close_partial_runtime(retriever, client)
        raise
    except QdrantServiceError as exc:
        _close_partial_runtime(retriever, client)
        message = (
            "Embedded Qdrant is unavailable. Use one process or configure "
            "Qdrant server mode."
            if active_corpus.storage_kind == "local"
            else "The active Qdrant collection is unavailable or incompatible."
        )
        raise RuntimeInitializationError("qdrant_not_ready", message) from exc
    except Exception as exc:
        _close_partial_runtime(retriever, client)
        raise RuntimeInitializationError(
            "runtime_dependency_initialization_failed",
            "Runtime dependencies could not be initialized.",
        ) from exc


def _default_qdrant_client_factory(active_corpus: ActiveCorpus):
    if active_corpus.storage_kind == "local":
        assert active_corpus.local_storage_path is not None
        try:
            return qdrant_client.QdrantClient(
                path=str(active_corpus.local_storage_path)
            )
        except Exception as exc:
            raise RuntimeInitializationError(
                "qdrant_local_unavailable",
                "Embedded Qdrant is unavailable. Use one process or configure "
                "Qdrant server mode.",
            ) from exc
    assert active_corpus.server_url
    try:
        return qdrant_client.QdrantClient(url=active_corpus.server_url)
    except Exception as exc:
        raise RuntimeInitializationError(
            "qdrant_server_unavailable",
            "The configured Qdrant server could not be initialized.",
        ) from exc


def _default_embedding_factory(settings: Settings) -> EmbeddingProvider:
    return FastEmbedProvider(settings.embedding_model, settings.embedding_dimension)


def _default_glm_factory(settings: Settings) -> GLMClient:
    return GLMClient(
        api_key=settings.glm_api_key,
        model=settings.glm_model,
        timeout_seconds=settings.glm_timeout_seconds,
        max_retries=settings.glm_max_retries,
    )


def _close_partial_runtime(
    retriever: QdrantService | None,
    client: Any | None,
) -> None:
    try:
        if retriever is not None:
            retriever.close()
        elif client is not None:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    except Exception:
        logger.exception("failed to close a partially initialized Qdrant client")


__all__ = [
    "RuntimeBuilder",
    "RuntimeContainer",
    "RuntimeFailure",
    "RuntimeInitializationError",
    "RuntimePhase",
    "RuntimeServices",
    "build_runtime_services",
]
