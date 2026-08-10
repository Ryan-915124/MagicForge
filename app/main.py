"""FastAPI entrypoint for MagicForge."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from analysis.analyzer import AnalysisFrameworkError
from analysis.models import MagicTheoryAnalysis
from app.audit_routes import router as audit_router
from app.auth_routes import router as auth_router
from app.config import MagicForgeProfile, Settings, get_settings
from app.database_runtime import (
    DatabaseManagerFactory,
    DatabaseRuntime,
    DatabaseRuntimeError,
    DatabaseRuntimePhase,
)
from app.governance_routes import router as governance_router
from app.governance_runtime import build_governance_service
from app.extraction_routes import router as extraction_router
from app.extraction_runtime import build_extraction_service
from app.mapping_routes import router as mapping_router
from app.mapping_runtime import build_mapping_service
from app.storage_routes import router as storage_router
from app.storage_runtime import build_storage_workflow_service
from app.knowledge_read_model import (
    KnowledgeReadModelError,
    ProjectionSummary,
)
from app.research_console_read_model import (
    ResearchConsoleReadModelError,
)
from app.runtime import (
    RuntimeBuilder,
    RuntimeContainer,
    RuntimeInitializationError,
    RuntimeServices,
    build_runtime_services,
)
from app.service import MagicForgeService
from app.reveal_parser import parse_magicforge_reveal
from app.security_runtime import (
    ProductActorDependency,
    ResearchActorDependency,
    build_security_services,
)
from llm.glm_client import GLMAPIError, GLMConfigurationError
from knowledge.governance import MagicForgeMode
from knowledge.bootstrap import BootstrapKnowledgeNodeCandidate
from knowledge.evidence import EvidenceCard
from knowledge.models import KnowledgeRelationship
from knowledge.projections import KnowledgeNodeVersion
from retrieval.interfaces import KnowledgeSearchFilter, SearchResult
from retrieval.qdrant_service import QdrantServiceError
from security.policy import retrieval_authorization_for_actor


class AssistantRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)


class AnalyzeRequest(BaseModel):
    magic_description: str = Field(min_length=1, max_length=20_000)


class CreateRequest(BaseModel):
    requirements: str = Field(min_length=1, max_length=20_000)


class SourceSummary(BaseModel):
    title: str
    author: str
    score: float
    document_id: str | None = None
    entity_ids: list[str] = Field(default_factory=list)
    source_locator: str | None = None
    page_number: int | None = None
    magic_category: str = ""
    artifact_type: str = ""
    knowledge_type: str = ""
    knowledge_origin: str = ""
    evidence_level: str = ""
    evidence_class: str = ""
    confidence: float | None = None
    confidence_label: str = ""
    limitations: list[str] = Field(default_factory=list)
    contradiction_status: str = ""
    evidence_card_id: str | None = None


class RevealActResponse(BaseModel):
    kind: Literal["effect", "hidden_structure", "cognitive_mechanism"]
    content: str


class GenerationResponse(BaseModel):
    result: str
    sources: list[SourceSummary]
    answer_format_version: Literal["magicforge.reveal.v1"] | None = None
    lead: str | None = None
    acts: list[RevealActResponse] | None = None
    synthesis: str | None = None


class AnalysisResponse(BaseModel):
    analysis: MagicTheoryAnalysis
    sources: list[SourceSummary]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    glm_configured: bool
    glm_connectivity: Literal["not_probed"] = "not_probed"
    qdrant_url: Literal["local", "remote"]
    collection: str
    mode: MagicForgeMode
    profile: MagicForgeProfile
    read_only: bool
    synthetic_corpus: bool
    corpus_id: str
    schema_version: str
    manifest_schema_version: str
    manifest_id: str


class LivenessResponse(BaseModel):
    status: Literal["alive"] = "alive"


class ReadinessResponse(BaseModel):
    status: Literal["ready"] = "ready"
    mode: MagicForgeMode
    profile: MagicForgeProfile
    read_only: bool
    synthetic_corpus: bool
    corpus_id: str
    schema_version: str
    manifest_schema_version: str
    manifest_id: str
    collection: str
    storage_kind: Literal["local", "remote"]
    glm_configured: bool
    glm_connectivity: Literal["not_probed"] = "not_probed"


class ProjectionSummaryResponse(BaseModel):
    run_id: str
    collection: str
    manifest_id: str
    generated_at: str
    sources: int
    knowledge_nodes: int
    relationships: int
    renderable_relationships: int
    evidence_cards: int
    qdrant_points: int
    bootstrap_generated: bool
    human_verified: bool

    @classmethod
    def from_summary(cls, summary: ProjectionSummary) -> "ProjectionSummaryResponse":
        return cls(**asdict(summary))


class KnowledgeSearchResponse(BaseModel):
    results: list[SearchResult] = Field(default_factory=list)
    evidence_cards: list[EvidenceCard] = Field(default_factory=list)
    nodes: list[BootstrapKnowledgeNodeCandidate | KnowledgeNodeVersion] = Field(
        default_factory=list
    )
    relationships: list[KnowledgeRelationship] = Field(default_factory=list)
    projection: ProjectionSummaryResponse


class CorpusDistributionsResponse(BaseModel):
    scope: str = "projected_points"
    artifact_types: dict[str, int]
    domain_memberships: dict[str, int]
    knowledge_origins: dict[str, int]
    knowledge_types: dict[str, int]


class CorpusGovernanceResponse(BaseModel):
    pending_human_review_sources: int
    contradiction_checks_pending: int
    procedural_method_projections_quarantined: int
    production_collection_touched: bool


class CorpusStatsResponse(BaseModel):
    run_id: str
    manifest_id: str
    generated_at: str
    mode: MagicForgeMode
    collection: str
    sources: int
    sources_with_projected_knowledge: int
    source_categories: dict[str, int]
    evidence_cards: int
    knowledge_nodes: int
    relationships: int
    renderable_relationships: int
    qdrant_points: int
    human_verified: int
    distributions: CorpusDistributionsResponse
    governance: CorpusGovernanceResponse


class ResearchApiRuntimeResponse(BaseModel):
    status: Literal["ok"]


class IntelligenceInstrumentResponse(BaseModel):
    provider: Literal["Zhipu GLM", "Offline deterministic Demo"]
    model: str
    configured: bool
    connectivity: Literal["not_probed"]
    structured_extraction: bool


class RetrievalRuntimeResponse(BaseModel):
    configured: bool
    collection: str
    storage_kind: Literal["local", "remote"]
    connectivity: Literal["not_probed"]


class ResearchRuntimeResponse(BaseModel):
    api: ResearchApiRuntimeResponse
    intelligence_instrument: IntelligenceInstrumentResponse
    retrieval: RetrievalRuntimeResponse


class CurrentResearchRunResponse(BaseModel):
    run_id: str
    mode: Literal["bootstrap"]
    generated_at: str
    collection: str
    status: Literal["receipt_verified"]


class PipelineStageResponse(BaseModel):
    id: str
    label: str
    status: Literal["completed", "receipt_verified"]
    metrics: dict[str, int]


class ResearchPipelineResponse(BaseModel):
    status: Literal["receipt_verified"]
    stages: list[PipelineStageResponse]


class MemoryManifestResponse(BaseModel):
    status: Literal["manifest_verified"]
    id: str
    hash: str
    point_count: int


class MemoryReceiptResponse(BaseModel):
    status: Literal["receipt_verified"]
    id: str
    ingested_at: str
    point_count: int


class RetrievalSmokeResponse(BaseModel):
    status: Literal["report_verified"]
    tested_at: str
    query_count: int
    collection_count: int
    all_returned_hits_bootstrap_safe: bool


class MemoryPointAlignmentResponse(BaseModel):
    manifest: int
    receipt: int
    smoke_observed: int


class MemorySafetyResponse(BaseModel):
    bootstrap_generated_points: int
    human_verified_points: int
    approved_points: int
    storage_permission_points: int
    production_collection_touched: bool
    production_collection_present_in_smoke: bool
    safety_excluded_projection_count: int


class MemoryVaultResponse(BaseModel):
    runtime_collection: str
    audited_collection: str
    alignment_status: Literal["aligned", "configuration_mismatch"]
    manifest: MemoryManifestResponse
    receipt: MemoryReceiptResponse
    retrieval_smoke: RetrievalSmokeResponse
    points: MemoryPointAlignmentResponse
    safety: MemorySafetyResponse


class ResearchGovernanceResponse(BaseModel):
    mode: Literal["bootstrap"]
    checkpoint_status: Literal["pending_human_review"]
    sources_pending: int
    evidence_cards_pending: int
    knowledge_nodes_pending: int
    relationships_pending: int
    contradiction_checks_pending: int
    procedural_method_projections_quarantined: int
    human_verified_points: int
    approved_points: int
    storage_permission_points: int


class ResearchRunHistoryResponse(BaseModel):
    run_id: str
    generated_at: str
    mode: Literal["bootstrap"]
    collection: str
    sources: int
    extracted_sources: int
    claims: int
    evidence_cards: int
    knowledge_nodes: int
    relationships: int
    qdrant_points: int
    extraction_errors: int
    status: Literal["reported_succeeded", "receipt_verified"]
    metric_basis: Literal[
        "reported_generated_outputs",
        "receipt_verified_projections",
    ]


class ResearchConsoleResponse(BaseModel):
    observed_at: str
    current_run: CurrentResearchRunResponse
    runtime: ResearchRuntimeResponse
    pipeline: ResearchPipelineResponse
    memory_vault: MemoryVaultResponse
    governance: ResearchGovernanceResponse
    run_history: list[ResearchRunHistoryResponse]


def _summarize_sources(results: list[SearchResult]) -> list[SourceSummary]:
    return [
        SourceSummary(
            title=str(result.payload.get("title") or "Untitled source"),
            author=str(result.payload.get("author") or "Unknown author"),
            score=result.score,
            document_id=(
                str(result.payload["document_id"])
                if result.payload.get("document_id")
                else None
            ),
            entity_ids=[str(item) for item in result.payload.get("entity_ids", [])],
            source_locator=(
                str(result.payload["source_locator"])
                if result.payload.get("source_locator")
                else None
            ),
            page_number=(
                int(result.payload["page_number"])
                if result.payload.get("page_number")
                else None
            ),
            magic_category=str(result.payload.get("magic_category") or ""),
            artifact_type=str(result.payload.get("artifact_type") or ""),
            knowledge_type=str(result.payload.get("knowledge_type") or ""),
            knowledge_origin=str(result.payload.get("knowledge_origin") or ""),
            evidence_level=str(result.payload.get("evidence_level") or ""),
            evidence_class=str(result.payload.get("evidence_class") or ""),
            confidence=(
                float(result.payload["confidence"])
                if result.payload.get("confidence") is not None
                else None
            ),
            confidence_label=str(result.payload.get("confidence_label") or ""),
            limitations=[
                str(item) for item in result.payload.get("limitations", [])
            ],
            contradiction_status=str(
                result.payload.get("contradiction_status") or ""
            ),
            evidence_card_id=(
                str(result.payload["evidence_card_id"])
                if result.payload.get("evidence_card_id")
                else None
            ),
        )
        for result in results
    ]


async def get_runtime_services(request: Request) -> RuntimeServices:
    database_runtime: DatabaseRuntime = request.app.state.database_runtime
    try:
        database_runtime.require_application_ready()
    except DatabaseRuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": exc.public_message},
        ) from exc
    container: RuntimeContainer = request.app.state.runtime_container
    try:
        return container.require_ready()
    except RuntimeInitializationError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": exc.public_message},
        ) from exc


RuntimeDependency = Annotated[RuntimeServices, Depends(get_runtime_services)]


async def get_magicforge_service(
    runtime: RuntimeDependency,
) -> MagicForgeService:
    return runtime.magicforge_service


ServiceDependency = Annotated[MagicForgeService, Depends(get_magicforge_service)]


def create_app(
    settings: Settings | None = None,
    runtime_builder: RuntimeBuilder | None = None,
    database_manager_factory: DatabaseManagerFactory | None = None,
) -> FastAPI:
    current_settings = settings or get_settings()
    is_demo = current_settings.effective_profile == MagicForgeProfile.DEMO

    database_runtime = DatabaseRuntime(
        # Demo never touches a configured governance database, including when
        # callers construct Settings directly instead of using from_env().
        database_url="" if is_demo else current_settings.database_url,
        required=current_settings.magicforge_mode == MagicForgeMode.PRODUCTION,
        allow_optional_failure=current_settings.anonymous_product_reads_enabled,
        connect_timeout_seconds=current_settings.database_connect_timeout_seconds,
        manager_factory=database_manager_factory,
    )

    def default_runtime_builder(runtime_settings: Settings) -> RuntimeServices:
        session_factory = None
        if runtime_settings.magicforge_mode == MagicForgeMode.PRODUCTION:
            session_factory = database_runtime.require_database_ready()
            if not callable(session_factory):
                raise RuntimeInitializationError(
                    "active_corpus_not_configured",
                    "Active corpus identity, manifest, receipt, and schema must be explicit.",
                )
        return build_runtime_services(
            runtime_settings,
            session_factory=session_factory,
        )

    container = RuntimeContainer(
        current_settings,
        runtime_builder or default_runtime_builder,
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        database_runtime.start()
        _application.state.security_services = None
        _application.state.governance_service = None
        _application.state.extraction_service = None
        _application.state.mapping_service = None
        _application.state.storage_workflow_service = None
        if database_runtime.phase == DatabaseRuntimePhase.READY:
            _application.state.security_services = build_security_services(
                database_runtime,
                current_settings,
            )
            _application.state.governance_service = build_governance_service(
                database_runtime
            )
            _application.state.extraction_service = build_extraction_service(
                database_runtime, current_settings
            )
            _application.state.mapping_service = build_mapping_service(
                database_runtime
            )
            _application.state.storage_workflow_service = (
                build_storage_workflow_service(database_runtime, current_settings)
            )
        if database_runtime.application_ready:
            container.start()
        try:
            yield
        finally:
            _application.state.governance_service = None
            _application.state.extraction_service = None
            _application.state.mapping_service = None
            storage_service = _application.state.storage_workflow_service
            _application.state.storage_workflow_service = None
            close_storage = getattr(storage_service, "close", None)
            if callable(close_storage):
                close_storage()
            _application.state.security_services = None
            container.close()
            database_runtime.close()

    application = FastAPI(
        title="MagicForge",
        version="0.1.0-alpha.2",
        description="A RAG-powered AI assistant for magicians.",
        lifespan=lifespan,
    )
    application.state.settings = current_settings
    application.state.runtime_container = container
    application.state.database_runtime = database_runtime
    application.state.security_services = None
    application.state.governance_service = None
    application.state.extraction_service = None
    application.state.mapping_service = None
    application.state.storage_workflow_service = None
    application.include_router(audit_router)
    application.include_router(auth_router)
    application.include_router(governance_router)
    application.include_router(extraction_router)
    application.include_router(mapping_router)
    application.include_router(storage_router)

    @application.get("/health/live", response_model=LivenessResponse)
    async def liveness() -> LivenessResponse:
        return LivenessResponse()

    @application.get("/health/ready")
    async def readiness():
        try:
            database_runtime.require_application_ready()
        except DatabaseRuntimeError as exc:
            return _database_error_response(exc)
        try:
            runtime = container.require_ready()
        except RuntimeInitializationError as exc:
            return _runtime_error_response(exc)
        corpus = runtime.active_corpus
        return ReadinessResponse(
            mode=corpus.mode,
            profile=current_settings.effective_profile,
            read_only=is_demo,
            synthetic_corpus=is_demo,
            corpus_id=corpus.corpus_id,
            schema_version=corpus.projection_schema_version,
            manifest_schema_version=corpus.manifest_schema_version,
            manifest_id=corpus.manifest_id,
            collection=corpus.collection_name,
            storage_kind=corpus.storage_kind,
            glm_configured=(False if is_demo else bool(current_settings.glm_api_key)),
        )

    @application.get("/health", response_model=HealthResponse)
    async def health():
        try:
            database_runtime.require_application_ready()
        except DatabaseRuntimeError as exc:
            return _database_error_response(exc)
        try:
            runtime = container.require_ready()
        except RuntimeInitializationError as exc:
            return _runtime_error_response(exc)
        corpus = runtime.active_corpus
        return HealthResponse(
            glm_configured=(False if is_demo else bool(current_settings.glm_api_key)),
            qdrant_url=corpus.storage_kind,
            collection=corpus.collection_name,
            mode=corpus.mode,
            profile=current_settings.effective_profile,
            read_only=is_demo,
            synthetic_corpus=is_demo,
            corpus_id=corpus.corpus_id,
            schema_version=corpus.projection_schema_version,
            manifest_schema_version=corpus.manifest_schema_version,
            manifest_id=corpus.manifest_id,
        )

    @application.post("/assistant", response_model=GenerationResponse)
    async def assistant(
        request: AssistantRequest,
        actor: ProductActorDependency,
        magicforge: ServiceDependency,
    ) -> GenerationResponse:
        authorization = retrieval_authorization_for_actor(actor)
        answer, sources = await run_in_threadpool(
            _invoke,
            magicforge.assistant,
            request.question,
            authorization,
        )
        reveal = parse_magicforge_reveal(answer)
        return GenerationResponse(
            result=answer,
            sources=_summarize_sources(sources),
            answer_format_version=("magicforge.reveal.v1" if reveal else None),
            lead=reveal.lead if reveal else None,
            acts=(
                [
                    RevealActResponse(kind=act.kind, content=act.content)
                    for act in reveal.acts
                ]
                if reveal
                else None
            ),
            synthesis=reveal.synthesis if reveal else None,
        )

    @application.post("/analyze", response_model=AnalysisResponse)
    async def analyze(
        request: AnalyzeRequest,
        actor: ProductActorDependency,
        magicforge: ServiceDependency,
    ) -> AnalysisResponse:
        authorization = retrieval_authorization_for_actor(actor)
        analysis, sources = await run_in_threadpool(
            _invoke,
            magicforge.analyze,
            request.magic_description,
            authorization,
        )
        return AnalysisResponse(
            analysis=analysis,
            sources=_summarize_sources(sources),
        )

    @application.post("/create", response_model=GenerationResponse)
    async def create(
        request: CreateRequest,
        actor: ProductActorDependency,
        magicforge: ServiceDependency,
    ) -> GenerationResponse:
        authorization = retrieval_authorization_for_actor(actor)
        answer, sources = await run_in_threadpool(
            _invoke,
            magicforge.create,
            request.requirements,
            authorization,
        )
        return GenerationResponse(result=answer, sources=_summarize_sources(sources))

    @application.get("/knowledge/search", response_model=KnowledgeSearchResponse)
    async def knowledge_search(
        actor: ProductActorDependency,
        runtime: RuntimeDependency,
        query: str = "",
        limit: int = Query(default=60, ge=1, le=120),
        knowledge_types: list[str] = Query(default=[]),
        domains: list[str] = Query(default=[]),
        ontology_paths: list[str] = Query(default=[]),
        knowledge_origins: list[str] = Query(default=[]),
        evidence_levels: list[str] = Query(default=[]),
        entity_ids: list[str] = Query(default=[]),
        entity_types: list[str] = Query(default=[]),
        relation_types: list[str] = Query(default=[]),
    ) -> KnowledgeSearchResponse:
        authorization = retrieval_authorization_for_actor(actor)
        try:
            page = runtime.knowledge_read_model.search(
                query=query,
                limit=limit,
                filters=KnowledgeSearchFilter(
                    knowledge_types=knowledge_types,
                    domains=domains,
                    ontology_paths=ontology_paths,
                    knowledge_origins=knowledge_origins,
                    evidence_levels=evidence_levels,
                    entity_ids=entity_ids,
                    entity_types=entity_types,
                    relation_types=relation_types,
                ),
                authorization=authorization,
            )
        except KnowledgeReadModelError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return KnowledgeSearchResponse(
            results=page.results,
            evidence_cards=page.evidence_cards,
            nodes=page.nodes,
            relationships=page.relationships,
            projection=ProjectionSummaryResponse.from_summary(page.projection),
        )

    @application.get(
        "/knowledge/node/{identifier}",
        response_model=BootstrapKnowledgeNodeCandidate | KnowledgeNodeVersion,
    )
    async def knowledge_node(
        identifier: str,
        actor: ProductActorDependency,
        runtime: RuntimeDependency,
    ) -> BootstrapKnowledgeNodeCandidate | KnowledgeNodeVersion:
        authorization = retrieval_authorization_for_actor(actor)
        try:
            node = runtime.knowledge_read_model.get_node(
                identifier,
                authorization=authorization,
            )
        except KnowledgeReadModelError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if node is None:
            raise HTTPException(status_code=404, detail="knowledge node not found")
        return node

    @application.get("/evidence/{identifier}", response_model=EvidenceCard)
    async def evidence_card(
        identifier: str,
        actor: ProductActorDependency,
        runtime: RuntimeDependency,
    ) -> EvidenceCard:
        authorization = retrieval_authorization_for_actor(actor)
        try:
            card = runtime.knowledge_read_model.get_evidence(
                identifier,
                authorization=authorization,
            )
        except KnowledgeReadModelError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if card is None:
            raise HTTPException(status_code=404, detail="Evidence Card not found")
        return card

    @application.get("/stats", response_model=CorpusStatsResponse)
    async def stats(
        actor: ProductActorDependency,
        runtime: RuntimeDependency,
    ) -> CorpusStatsResponse:
        del actor
        try:
            read_model = runtime.knowledge_read_model
            summary = read_model.summary
            statistics = read_model.statistics
        except KnowledgeReadModelError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return CorpusStatsResponse(
            run_id=summary.run_id,
            manifest_id=summary.manifest_id,
            generated_at=summary.generated_at,
            mode=runtime.active_corpus.mode,
            collection=summary.collection,
            sources=summary.sources,
            sources_with_projected_knowledge=(
                statistics.sources_with_projected_knowledge
            ),
            source_categories=statistics.source_categories,
            evidence_cards=summary.evidence_cards,
            knowledge_nodes=summary.knowledge_nodes,
            relationships=summary.relationships,
            renderable_relationships=summary.renderable_relationships,
            qdrant_points=summary.qdrant_points,
            human_verified=statistics.human_verified_points,
            distributions=CorpusDistributionsResponse(
                artifact_types=statistics.artifact_types,
                domain_memberships=statistics.domain_memberships,
                knowledge_origins=statistics.knowledge_origins,
                knowledge_types=statistics.knowledge_types,
            ),
            governance=CorpusGovernanceResponse(
                pending_human_review_sources=(
                    statistics.pending_human_review_sources
                ),
                contradiction_checks_pending=(
                    statistics.contradiction_checks_pending
                ),
                procedural_method_projections_quarantined=(
                    statistics.procedural_method_projections_quarantined
                ),
                production_collection_touched=(
                    statistics.production_collection_touched
                ),
            ),
        )

    @application.get("/research/console", response_model=ResearchConsoleResponse)
    async def research_console(
        actor: ResearchActorDependency,
        runtime: RuntimeDependency,
    ) -> ResearchConsoleResponse:
        """Return configured runtime state and audited research artifacts.

        This endpoint intentionally performs no GLM or Qdrant network probe.
        """

        del actor
        try:
            snapshot = runtime.research_console_read_model.snapshot(current_settings)
        except ResearchConsoleReadModelError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return ResearchConsoleResponse.model_validate(asdict(snapshot))

    return application


def _runtime_error_response(exc: RuntimeInitializationError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "code": exc.code,
                "message": exc.public_message,
            }
        },
    )


def _database_error_response(exc: DatabaseRuntimeError) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "code": exc.code,
                "message": exc.public_message,
            }
        },
    )


def _invoke(
    operation,
    user_input: str,
    authorization,
) -> tuple[Any, list[SearchResult]]:
    try:
        return operation(user_input, authorization=authorization)
    except GLMConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except QdrantServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeInitializationError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": exc.public_message},
        ) from exc
    except GLMAPIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AnalysisFrameworkError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


app = create_app()
