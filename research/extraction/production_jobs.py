"""Application service for governed Production GLM extraction jobs.

This service owns readiness discovery and durable queue creation. Execution is
implemented as a separate transition below so merely queuing work never sends
Source text to an external provider.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.config import Settings
from knowledge.governance import MagicForgeMode, SensitiveInformationLevel
from llm.glm_client import GLMAPIError, GLMClient, GLMConfigurationError
from persistence.audit import AuditEventRepository
from persistence.extraction_jobs import (
    ExtractionJobRepository,
    ExtractionPersistenceError,
)
from persistence.idempotency import canonical_payload_hash
from persistence.models import (
    ExtractionAttemptStatus,
    ExtractionJob,
    ExtractionJobStatus,
    RoleName,
    SessionStatus,
    UserStatus,
)
from persistence.uow import SqlAlchemyUnitOfWork
from research.extraction.extractor import (
    ResearchExtractionError,
    ResearchKnowledgeExtractor,
)
from research.extraction.models import (
    EXTRACTION_SCHEMA_VERSION,
    EntityMappingProposal,
    ExtractedClaim,
    RelationshipMappingProposal,
    ResearchExtractionResult,
)
from research.extraction.production_bridge import (
    ProductionBridgeError,
    ProductionClaimSubmission,
    build_production_claim_submissions,
)
from research.extraction.production_source import (
    EligibleSourceView,
    ProductionGLMOutboundPolicy,
    ProductionSourceAdapter,
    ProductionSourceReadinessError,
)
from research.review.workflow_models import (
    ExtractionProducer,
    ExtractionProvenanceInput,
)
from research.review.workflow_service import (
    GovernanceServiceError,
    ProductionGovernanceService,
)
from security.policy import (
    AuthenticatedActor,
    Permission,
    has_permission,
)
from security.repositories import RoleRepository, SessionRepository, UserRepository


PRODUCTION_EXTRACTION_JOB_SCHEMA_VERSION = "production-extraction-job-0.1"
PRODUCTION_EXTRACTION_PROMPT_VERSION = "magic-research-extraction-0.2.1"
PRODUCTION_EXTRACTION_ARTIFACT_SCHEMA_VERSION = (
    "production-extraction-artifact-0.1"
)


class ProductionExtractionServiceError(RuntimeError):
    status_code = 409

    def __init__(self, code: str, public_message: str) -> None:
        self.code = code
        self.public_message = public_message
        super().__init__(public_message)


class ProductionExtractionAuthorizationError(ProductionExtractionServiceError):
    status_code = 403


class ProductionExtractionNotFoundError(ProductionExtractionServiceError):
    status_code = 404


class ProductionExtractionExternalError(ProductionExtractionServiceError):
    status_code = 502


class ProductionExtractionConfigurationError(ProductionExtractionServiceError):
    status_code = 503


class ProductionExtractionInternalError(ProductionExtractionServiceError):
    status_code = 500


class ExtractionJobCreateCommand(BaseModel):
    """The operator chooses only a Source; model/config remain server-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_version_id: UUID


class ExtractionAttemptView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_number: int = Field(ge=1)
    status: ExtractionAttemptStatus
    result_checksum: str | None = None
    error_code: str | None = None
    started_at: datetime
    finished_at: datetime


class ExtractionJobView(BaseModel):
    """Content-free operator view; snapshots intentionally exclude Source text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    source_version_id: UUID
    source_review_decision_id: UUID
    source_permission_request_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    sensitivity: SensitiveInformationLevel
    provider: str
    model: str
    prompt_version: str
    output_schema_version: str
    configuration_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ExtractionJobStatus
    attempt_count: int = Field(ge=0)
    result_checksum: str | None = None
    error_code: str | None = None
    requested_by_user_id: UUID
    created_at: datetime
    updated_at: datetime
    attempts: tuple[ExtractionAttemptView, ...] = ()


class ExtractionJobCreateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job: ExtractionJobView
    created: bool


class ExtractionJobRunResult(BaseModel):
    """Content-free execution receipt; proposal details remain in review queues."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job: ExtractionJobView
    proposal_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim_candidates: int = Field(ge=0)
    context_claims: int = Field(ge=0)
    entity_proposals: int = Field(ge=0)
    relationship_proposals: int = Field(ge=0)
    artifact_reused: bool


@dataclass(frozen=True, slots=True)
class _ExecutionJobSnapshot:
    id: UUID
    source_version_id: UUID
    source_review_decision_id: UUID
    source_permission_request_id: UUID
    content_hash: str
    model_name: str
    prompt_version: str
    output_schema_version: str
    chunk_size: int
    chunk_overlap: int
    status: ExtractionJobStatus


class _ExtractionArtifactPayload(BaseModel):
    """Validated, source-text-free proposal saved before Claim submission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_schema_version: Literal["production-extraction-artifact-0.1"] = (
        PRODUCTION_EXTRACTION_ARTIFACT_SCHEMA_VERSION
    )
    job_id: UUID
    source_version_id: UUID
    source_review_decision_id: UUID
    source_permission_request_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_id: UUID
    extraction_run_id: UUID
    extraction_schema_version: str
    claim_submissions: tuple[ProductionClaimSubmission, ...] = ()
    context_claims: tuple[ExtractedClaim, ...] = ()
    entity_proposals: tuple[EntityMappingProposal, ...] = ()
    relationship_proposals: tuple[RelationshipMappingProposal, ...] = ()
    limitations: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    temporary_chunk_count: int = Field(ge=1)


@dataclass(frozen=True, slots=True)
class _LoadedArtifact:
    payload: _ExtractionArtifactPayload
    checksum: str


ExtractorFactory = Callable[[_ExecutionJobSnapshot, Settings], ResearchKnowledgeExtractor]
GovernanceFactory = Callable[[Any], ProductionGovernanceService]


class ProductionExtractionService:
    """Queue and inspect GLM work without approving downstream knowledge."""

    def __init__(
        self,
        session_factory,
        settings: Settings,
        *,
        extractor_factory: ExtractorFactory | None = None,
        governance_factory: GovernanceFactory = ProductionGovernanceService,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._sources = ProductionSourceAdapter(session_factory)
        self._extractor_factory = extractor_factory or self._default_extractor
        self._governance = governance_factory(session_factory)

    def list_eligible_sources(
        self,
        actor: AuthenticatedActor,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EligibleSourceView]:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            self._require_live_operator(unit.session, actor)
        return self._sources.list_eligible(limit=limit, offset=offset)

    def queue(
        self,
        actor: AuthenticatedActor,
        command: ExtractionJobCreateCommand,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ExtractionJobCreateResult:
        try:
            envelope = self._sources.load(command.source_version_id)
            # Queueing is permitted while the runtime execution switch is off,
            # but all other outbound restrictions must already pass.
            ProductionGLMOutboundPolicy.require_allowed(
                envelope, extraction_enabled=True
            )
        except ProductionSourceReadinessError as exc:
            raise ProductionExtractionServiceError(
                exc.code, exc.public_message
            ) from exc

        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_live_operator(unit.session, actor)
            try:
                job, created = ExtractionJobRepository(unit.session).create_job(
                    source_version_id=envelope.source_version_id,
                    source_review_decision_id=envelope.source_review_decision_id,
                    source_permission_request_id=envelope.source_permission_request_id,
                    requested_by_user_id=current.user_id,
                    model_name=self._settings.glm_model,
                    model_snapshot={
                        "provider": "GLM",
                        "model": self._settings.glm_model,
                        "structured_output": True,
                        "temperature": 0.2,
                    },
                    prompt_version=PRODUCTION_EXTRACTION_PROMPT_VERSION,
                    prompt_snapshot={
                        "contract": "untrusted-source-evidence-only",
                        "claim_role_gate": "0.2",
                        "entity_validation_gate": "0.2",
                        "relationship_entailment_gate": "0.2",
                    },
                    output_schema_version=EXTRACTION_SCHEMA_VERSION,
                    schema_snapshot=ResearchExtractionResult.model_json_schema(),
                    chunk_config_snapshot={
                        "size": self._settings.chunk_size,
                        "overlap": self._settings.chunk_overlap,
                    },
                    now=self._now(),
                )
            except ExtractionPersistenceError as exc:
                raise ProductionExtractionServiceError(
                    "extraction_job_rejected",
                    "The extraction job failed its governance integrity checks.",
                ) from exc
            if created:
                AuditEventRepository(unit.session).append(
                    event_type="extraction_job_queued",
                    actor_user_id=current.user_id,
                    actor_role_snapshot=roles,
                    object_type="extraction_job",
                    object_id=str(job.id),
                    new_state={
                        "source_version_id": str(job.source_version_id),
                        "source_review_decision_id": str(
                            job.source_review_decision_id
                        ),
                        "source_permission_request_id": str(
                            job.source_permission_request_id
                        ),
                        "configuration_checksum": job.configuration_checksum,
                        "status": job.status.value,
                    },
                    reason="Queue an exact approved Source version for governed GLM extraction.",
                    request_id=request_id,
                    correlation_id=correlation_id,
                    payload_checksum=job.configuration_checksum,
                )
            view = self._job_view(unit.session, job)
            unit.commit()
            return ExtractionJobCreateResult(job=view, created=created)

    def run(
        self,
        actor: AuthenticatedActor,
        job_id: UUID,
        *,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ExtractionJobRunResult:
        """Execute one exact approved-source job and submit Claims for review.

        The external call runs outside a database transaction.  Its validated,
        source-text-free proposal is persisted before any Claim is submitted,
        so a partial failure can resume without sending the Source to GLM again.
        No Evidence Card, mapping, manifest, or Qdrant write occurs here.
        """

        snapshot = self._execution_snapshot(actor, job_id)
        if snapshot.status == ExtractionJobStatus.SUCCEEDED:
            artifact = self._load_artifact(job_id)
            if artifact is None:
                raise ProductionExtractionInternalError(
                    "extraction_artifact_unavailable",
                    "The completed extraction receipt failed its integrity check.",
                )
            self._validate_artifact_binding(snapshot, artifact.payload)
            return self._run_result(
                actor, artifact, artifact_reused=True
            )
        if snapshot.status == ExtractionJobStatus.RUNNING:
            raise ProductionExtractionServiceError(
                "extraction_job_already_running",
                "The extraction job already has an active attempt.",
            )

        try:
            envelope = self._sources.load(snapshot.source_version_id)
            ProductionGLMOutboundPolicy.require_allowed(
                envelope,
                extraction_enabled=self._settings.production_glm_extraction_enabled,
            )
            self._validate_envelope_binding(snapshot, envelope)
        except ProductionSourceReadinessError as exc:
            raise ProductionExtractionServiceError(
                exc.code, exc.public_message
            ) from exc

        attempt_number, runner, runner_roles = self._start_attempt(
            actor,
            snapshot,
            request_id=request_id,
            correlation_id=correlation_id,
        )
        try:
            artifact = self._load_artifact(job_id)
            artifact_reused = artifact is not None
            if artifact is None:
                extractor = self._extractor_factory(snapshot, self._settings)
                proposal = extractor.extract_candidate(
                    envelope.candidate,
                    envelope.citation,
                    envelope.approval,
                )

                # Approval and sensitivity may change while GLM is running.
                # Revalidate the exact chain before persisting or submitting.
                current_envelope = self._sources.load(snapshot.source_version_id)
                ProductionGLMOutboundPolicy.require_allowed(
                    current_envelope,
                    extraction_enabled=(
                        self._settings.production_glm_extraction_enabled
                    ),
                )
                self._validate_envelope_binding(snapshot, current_envelope)
                provenance = ExtractionProvenanceInput(
                    producer=ExtractionProducer.GLM,
                    extractor="ResearchKnowledgeExtractor",
                    extraction_schema_version=proposal.schema_version,
                    run_id=proposal.extraction_run_id,
                    llm_provider="GLM",
                    model=snapshot.model_name,
                    tool_version=PRODUCTION_EXTRACTION_JOB_SCHEMA_VERSION,
                )
                submissions = build_production_claim_submissions(
                    proposal,
                    extraction_provenance=provenance,
                )
                payload = _ExtractionArtifactPayload(
                    job_id=snapshot.id,
                    source_version_id=snapshot.source_version_id,
                    source_review_decision_id=(
                        snapshot.source_review_decision_id
                    ),
                    source_permission_request_id=(
                        snapshot.source_permission_request_id
                    ),
                    content_hash=snapshot.content_hash,
                    proposal_id=UUID(proposal.id),
                    extraction_run_id=UUID(proposal.extraction_run_id),
                    extraction_schema_version=proposal.schema_version,
                    claim_submissions=submissions,
                    context_claims=tuple(proposal.context_claims),
                    entity_proposals=tuple(proposal.entity_proposals),
                    relationship_proposals=tuple(
                        proposal.relationship_proposals
                    ),
                    limitations=tuple(proposal.limitations),
                    conflicts=tuple(proposal.conflicts),
                    temporary_chunk_count=proposal.temporary_chunk_count,
                )
                artifact = self._persist_artifact(
                    runner,
                    snapshot,
                    attempt_number,
                    payload,
                    request_id=request_id,
                    correlation_id=correlation_id,
                )
            else:
                self._validate_artifact_binding(snapshot, artifact.payload)

            for submission in artifact.payload.claim_submissions:
                self._governance.submit_claim(
                    runner,
                    submission.command,
                    idempotency_key=submission.idempotency_key,
                    request_id=request_id,
                    correlation_id=correlation_id,
                )

            self._succeed_attempt(
                runner,
                runner_roles,
                snapshot,
                attempt_number,
                artifact.checksum,
                request_id=request_id,
                correlation_id=correlation_id,
            )
            return self._run_result(
                runner, artifact, artifact_reused=artifact_reused
            )
        except Exception as exc:
            error_code, public_error = self._execution_failure(exc)
            self._fail_attempt(
                runner,
                runner_roles,
                snapshot,
                attempt_number,
                error_code,
                request_id=request_id,
                correlation_id=correlation_id,
            )
            raise public_error from exc

    def get_job(
        self, actor: AuthenticatedActor, job_id: UUID
    ) -> ExtractionJobView:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            self._require_live_operator(unit.session, actor)
            job = ExtractionJobRepository(unit.session).get(job_id)
            if job is None:
                raise ProductionExtractionNotFoundError(
                    "extraction_job_not_found", "The extraction job is unavailable."
                )
            return self._job_view(unit.session, job)

    def list_jobs(
        self,
        actor: AuthenticatedActor,
        *,
        status: ExtractionJobStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExtractionJobView]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ProductionExtractionServiceError(
                "invalid_pagination", "Pagination parameters are invalid."
            )
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            self._require_live_operator(unit.session, actor)
            statement = select(ExtractionJob)
            if status is not None:
                statement = statement.where(ExtractionJob.status == status)
            statement = statement.order_by(
                ExtractionJob.created_at.desc(), ExtractionJob.id.desc()
            ).offset(offset).limit(limit)
            return [
                self._job_view(unit.session, row)
                for row in unit.session.scalars(statement)
            ]

    def _execution_snapshot(
        self, actor: AuthenticatedActor, job_id: UUID
    ) -> _ExecutionJobSnapshot:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            self._require_live_operator(unit.session, actor)
            job = ExtractionJobRepository(unit.session).get(job_id)
            if job is None:
                raise ProductionExtractionNotFoundError(
                    "extraction_job_not_found", "The extraction job is unavailable."
                )
            if (
                job.llm_provider != "GLM"
                or job.prompt_version != PRODUCTION_EXTRACTION_PROMPT_VERSION
                or job.output_schema_version != EXTRACTION_SCHEMA_VERSION
            ):
                raise ProductionExtractionServiceError(
                    "extraction_job_configuration_obsolete",
                    "The queued extraction configuration is no longer executable.",
                )
            try:
                chunk_size = _strict_positive_int(
                    job.chunk_config_snapshot.get("size"), "chunk size"
                )
                chunk_overlap = _strict_nonnegative_int(
                    job.chunk_config_snapshot.get("overlap"), "chunk overlap"
                )
            except ValueError as exc:
                raise ProductionExtractionServiceError(
                    "extraction_job_configuration_invalid",
                    "The queued extraction configuration failed validation.",
                ) from exc
            if chunk_overlap >= chunk_size:
                raise ProductionExtractionServiceError(
                    "extraction_job_configuration_invalid",
                    "The queued extraction configuration failed validation.",
                )
            return _ExecutionJobSnapshot(
                id=job.id,
                source_version_id=job.source_version_id,
                source_review_decision_id=job.source_review_decision_id,
                source_permission_request_id=job.source_permission_request_id,
                content_hash=job.content_hash,
                model_name=job.model_name,
                prompt_version=job.prompt_version,
                output_schema_version=job.output_schema_version,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                status=job.status,
            )

    def _start_attempt(
        self,
        actor: AuthenticatedActor,
        snapshot: _ExecutionJobSnapshot,
        *,
        request_id: str | None,
        correlation_id: str | None,
    ) -> tuple[int, AuthenticatedActor, frozenset[RoleName]]:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_live_operator(unit.session, actor)
            repository = ExtractionJobRepository(unit.session)
            try:
                attempt_number = repository.start_attempt(
                    snapshot.id, now=self._now()
                )
            except ExtractionPersistenceError as exc:
                raise ProductionExtractionServiceError(
                    "extraction_attempt_rejected",
                    "The extraction job could not start a new attempt.",
                ) from exc
            AuditEventRepository(unit.session).append(
                event_type="extraction_attempt_started",
                actor_user_id=current.user_id,
                actor_role_snapshot=roles,
                object_type="extraction_job",
                object_id=str(snapshot.id),
                new_state={
                    "attempt_number": attempt_number,
                    "status": ExtractionJobStatus.RUNNING.value,
                    "source_version_id": str(snapshot.source_version_id),
                    "configuration": {
                        "provider": "GLM",
                        "model": snapshot.model_name,
                        "prompt_version": snapshot.prompt_version,
                        "schema_version": snapshot.output_schema_version,
                    },
                },
                reason="Start governed GLM extraction for an exact approved Source version.",
                request_id=request_id,
                correlation_id=correlation_id,
                payload_checksum=snapshot.content_hash,
            )
            unit.commit()
            return attempt_number, current, roles

    def _load_artifact(self, job_id: UUID) -> _LoadedArtifact | None:
        with self._session_factory() as session:
            try:
                row = ExtractionJobRepository(session).get_proposal_artifact(job_id)
                if row is None:
                    return None
                payload = _ExtractionArtifactPayload.model_validate(row.payload)
            except (ExtractionPersistenceError, ValueError) as exc:
                raise ProductionExtractionInternalError(
                    "extraction_artifact_invalid",
                    "The extraction proposal artifact failed its integrity check.",
                ) from exc
            return _LoadedArtifact(payload=payload, checksum=row.payload_checksum)

    def _persist_artifact(
        self,
        actor: AuthenticatedActor,
        snapshot: _ExecutionJobSnapshot,
        attempt_number: int,
        payload: _ExtractionArtifactPayload,
        *,
        request_id: str | None,
        correlation_id: str | None,
    ) -> _LoadedArtifact:
        serialized = payload.model_dump(mode="json")
        checksum = canonical_payload_hash(serialized)
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_live_operator(unit.session, actor)
            repository = ExtractionJobRepository(unit.session)
            job = repository.get(snapshot.id, for_update=True)
            if (
                job is None
                or job.status != ExtractionJobStatus.RUNNING
                or job.attempt_count != attempt_number
            ):
                raise ProductionExtractionServiceError(
                    "extraction_attempt_not_active",
                    "The extraction attempt is no longer active.",
                )
            try:
                row, created = repository.store_proposal_artifact(
                    snapshot.id,
                    proposal_id=payload.proposal_id,
                    extraction_run_id=str(payload.extraction_run_id),
                    schema_version=payload.extraction_schema_version,
                    payload=serialized,
                    payload_checksum=checksum,
                    now=self._now(),
                )
            except ExtractionPersistenceError as exc:
                raise ProductionExtractionInternalError(
                    "extraction_artifact_rejected",
                    "The extraction proposal artifact failed persistence checks.",
                ) from exc
            if created:
                AuditEventRepository(unit.session).append(
                    event_type="extraction_proposal_persisted",
                    actor_user_id=current.user_id,
                    actor_role_snapshot=roles,
                    object_type="extraction_job",
                    object_id=str(snapshot.id),
                    new_state={
                        "attempt_number": attempt_number,
                        "proposal_id": str(payload.proposal_id),
                        "claim_candidates": len(payload.claim_submissions),
                        "context_claims": len(payload.context_claims),
                        "entity_proposals": len(payload.entity_proposals),
                        "relationship_proposals": len(
                            payload.relationship_proposals
                        ),
                        "payload_checksum": row.payload_checksum,
                    },
                    reason="Persist validated proposal before Claim review submissions.",
                    request_id=request_id,
                    correlation_id=correlation_id,
                    payload_checksum=row.payload_checksum,
                )
            unit.commit()
            return _LoadedArtifact(payload=payload, checksum=row.payload_checksum)

    def _succeed_attempt(
        self,
        actor: AuthenticatedActor,
        roles: frozenset[RoleName],
        snapshot: _ExecutionJobSnapshot,
        attempt_number: int,
        result_checksum: str,
        *,
        request_id: str | None,
        correlation_id: str | None,
    ) -> None:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            repository = ExtractionJobRepository(unit.session)
            try:
                repository.succeed_attempt(
                    snapshot.id,
                    attempt_number=attempt_number,
                    result_checksum=result_checksum,
                    now=self._now(),
                )
            except ExtractionPersistenceError as exc:
                raise ProductionExtractionInternalError(
                    "extraction_completion_rejected",
                    "The extraction completion failed its integrity checks.",
                ) from exc
            AuditEventRepository(unit.session).append(
                event_type="extraction_attempt_succeeded",
                actor_user_id=actor.user_id,
                actor_role_snapshot=roles,
                object_type="extraction_job",
                object_id=str(snapshot.id),
                previous_state={"status": ExtractionJobStatus.RUNNING.value},
                new_state={
                    "status": ExtractionJobStatus.SUCCEEDED.value,
                    "attempt_number": attempt_number,
                    "result_checksum": result_checksum,
                },
                reason="Complete governed extraction after pending Claim submission.",
                request_id=request_id,
                correlation_id=correlation_id,
                payload_checksum=result_checksum,
            )
            unit.commit()

    def _fail_attempt(
        self,
        actor: AuthenticatedActor,
        roles: frozenset[RoleName],
        snapshot: _ExecutionJobSnapshot,
        attempt_number: int,
        error_code: str,
        *,
        request_id: str | None,
        correlation_id: str | None,
    ) -> None:
        """Best-effort terminalization that records only a safe machine code."""

        try:
            with SqlAlchemyUnitOfWork(self._session_factory) as unit:
                ExtractionJobRepository(unit.session).fail_attempt(
                    snapshot.id,
                    attempt_number=attempt_number,
                    error_code=error_code,
                    now=self._now(),
                )
                AuditEventRepository(unit.session).append(
                    event_type="extraction_attempt_failed",
                    actor_user_id=actor.user_id,
                    actor_role_snapshot=roles,
                    object_type="extraction_job",
                    object_id=str(snapshot.id),
                    previous_state={"status": ExtractionJobStatus.RUNNING.value},
                    new_state={
                        "status": ExtractionJobStatus.FAILED.value,
                        "attempt_number": attempt_number,
                        "error_code": error_code,
                    },
                    reason="Fail governed extraction without persisting exception text.",
                    request_id=request_id,
                    correlation_id=correlation_id,
                )
                unit.commit()
        except Exception:
            # Never replace the safe public execution error with a persistence
            # exception that might include database or provider details.
            return

    def _run_result(
        self,
        actor: AuthenticatedActor,
        artifact: _LoadedArtifact,
        *,
        artifact_reused: bool,
    ) -> ExtractionJobRunResult:
        payload = artifact.payload
        return ExtractionJobRunResult(
            job=self.get_job(actor, payload.job_id),
            proposal_checksum=artifact.checksum,
            claim_candidates=len(payload.claim_submissions),
            context_claims=len(payload.context_claims),
            entity_proposals=len(payload.entity_proposals),
            relationship_proposals=len(payload.relationship_proposals),
            artifact_reused=artifact_reused,
        )

    @staticmethod
    def _validate_envelope_binding(snapshot, envelope) -> None:
        if (
            envelope.source_version_id != snapshot.source_version_id
            or envelope.source_review_decision_id
            != snapshot.source_review_decision_id
            or envelope.source_permission_request_id
            != snapshot.source_permission_request_id
            or envelope.approval.content_hash != snapshot.content_hash
        ):
            raise ProductionSourceReadinessError(
                "source_approval_changed",
                "The Source approval chain changed after this job was queued.",
            )

    @staticmethod
    def _validate_artifact_binding(
        snapshot: _ExecutionJobSnapshot,
        payload: _ExtractionArtifactPayload,
    ) -> None:
        if (
            payload.job_id != snapshot.id
            or payload.source_version_id != snapshot.source_version_id
            or payload.source_review_decision_id
            != snapshot.source_review_decision_id
            or payload.source_permission_request_id
            != snapshot.source_permission_request_id
            or payload.content_hash != snapshot.content_hash
            or payload.extraction_schema_version != snapshot.output_schema_version
        ):
            raise ProductionExtractionInternalError(
                "extraction_artifact_binding_invalid",
                "The extraction proposal artifact failed its binding checks.",
            )

    @staticmethod
    def _execution_failure(
        exc: Exception,
    ) -> tuple[str, ProductionExtractionServiceError]:
        if isinstance(exc, GLMConfigurationError):
            return (
                "glm_configuration_error",
                ProductionExtractionConfigurationError(
                    "glm_configuration_error",
                    "GLM extraction is not configured for execution.",
                ),
            )
        if isinstance(exc, GLMAPIError):
            return (
                "glm_request_failed",
                ProductionExtractionExternalError(
                    "glm_request_failed",
                    "GLM extraction failed without producing review material.",
                ),
            )
        if isinstance(exc, ResearchExtractionError):
            return (
                "extraction_validation_failed",
                ProductionExtractionServiceError(
                    "extraction_validation_failed",
                    "The extracted proposal failed semantic validation.",
                ),
            )
        if isinstance(exc, ProductionBridgeError):
            return (
                "claim_bridge_rejected",
                ProductionExtractionServiceError(
                    "claim_bridge_rejected",
                    "The extracted proposal could not enter Claim review.",
                ),
            )
        if isinstance(exc, ProductionSourceReadinessError):
            return (
                "source_approval_changed",
                ProductionExtractionServiceError(
                    "source_approval_changed",
                    "The Source approval changed during extraction.",
                ),
            )
        if isinstance(exc, GovernanceServiceError):
            return (
                "claim_submission_rejected",
                ProductionExtractionServiceError(
                    "claim_submission_rejected",
                    "The generated Claims could not enter the human review queue.",
                ),
            )
        if isinstance(exc, ProductionExtractionServiceError):
            return exc.code, exc
        if isinstance(exc, ExtractionPersistenceError):
            return (
                "extraction_persistence_failed",
                ProductionExtractionInternalError(
                    "extraction_persistence_failed",
                    "Extraction persistence failed its integrity checks.",
                ),
            )
        return (
            "unexpected_extraction_failure",
            ProductionExtractionInternalError(
                "unexpected_extraction_failure",
                "Extraction failed without producing approved knowledge.",
            ),
        )

    @staticmethod
    def _default_extractor(
        snapshot: _ExecutionJobSnapshot, settings: Settings
    ) -> ResearchKnowledgeExtractor:
        return ResearchKnowledgeExtractor(
            GLMClient(
                api_key=settings.glm_api_key,
                model=snapshot.model_name,
                timeout_seconds=settings.glm_timeout_seconds,
                max_retries=settings.glm_max_retries,
            ),
            chunk_size=snapshot.chunk_size,
            chunk_overlap=snapshot.chunk_overlap,
            mode=MagicForgeMode.PRODUCTION,
        )

    def _job_view(self, session, job: ExtractionJob) -> ExtractionJobView:
        attempts = ExtractionJobRepository(session).list_attempts(job.id)
        return ExtractionJobView(
            id=job.id,
            source_version_id=job.source_version_id,
            source_review_decision_id=job.source_review_decision_id,
            source_permission_request_id=job.source_permission_request_id,
            content_hash=job.content_hash,
            sensitivity=job.sensitivity,
            provider=job.llm_provider,
            model=job.model_name,
            prompt_version=job.prompt_version,
            output_schema_version=job.output_schema_version,
            configuration_checksum=job.configuration_checksum,
            status=job.status,
            attempt_count=job.attempt_count,
            result_checksum=job.result_checksum,
            error_code=job.error_code,
            requested_by_user_id=job.requested_by_user_id,
            created_at=self._aware(job.created_at),
            updated_at=self._aware(job.updated_at),
            attempts=tuple(
                ExtractionAttemptView(
                    attempt_number=row.attempt_number,
                    status=row.status,
                    result_checksum=row.result_checksum,
                    error_code=row.error_code,
                    started_at=self._aware(row.started_at),
                    finished_at=self._aware(row.finished_at),
                )
                for row in attempts
            ),
        )

    def _require_live_operator(
        self, session, actor: AuthenticatedActor
    ) -> tuple[AuthenticatedActor, frozenset[RoleName]]:
        if actor.user_id is None or actor.session_id is None or actor.is_bootstrap_anonymous:
            raise ProductionExtractionAuthorizationError(
                "extraction_operator_required",
                "A live operator session is required for extraction jobs.",
            )
        session_record = SessionRepository(session).get(actor.session_id)
        user = UserRepository(session).get(actor.user_id)
        now = self._now()
        if (
            session_record is None
            or session_record.user_id != actor.user_id
            or session_record.status != SessionStatus.ACTIVE
            or self._aware(session_record.expires_at) <= now
            or session_record.transport != actor.session_transport
            or user is None
            or user.status != UserStatus.ACTIVE
        ):
            raise ProductionExtractionAuthorizationError(
                "extraction_operator_required",
                "A live operator session is required for extraction jobs.",
            )
        roles = RoleRepository(session).active_names_for_user(user.id)
        current = AuthenticatedActor(
            user_id=user.id,
            username=user.username,
            roles=roles,
            session_id=session_record.id,
            session_transport=session_record.transport,
            session_expires_at=self._aware(session_record.expires_at),
        )
        if not has_permission(current, Permission.EXTRACTION_RUN):
            raise ProductionExtractionAuthorizationError(
                "extraction_operator_required",
                "A live operator session is required for extraction jobs.",
            )
        return current, roles

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)


def _strict_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _strict_nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


__all__ = [
    "ExtractionAttemptView",
    "ExtractionJobCreateCommand",
    "ExtractionJobCreateResult",
    "ExtractionJobRunResult",
    "ExtractionJobView",
    "ProductionExtractionAuthorizationError",
    "ProductionExtractionConfigurationError",
    "ProductionExtractionExternalError",
    "ProductionExtractionInternalError",
    "ProductionExtractionNotFoundError",
    "ProductionExtractionService",
    "ProductionExtractionServiceError",
]
