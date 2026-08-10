"""Transactional Storage Manifest, ingestion saga, and corpus activation.

No request model contains projection JSON.  The production resolver reloads
exact approved relational versions and reconstructs projections server-side.
External Qdrant work occurs outside a database transaction and is finalized by
a second transaction, with cleanup/reconciliation persisted on failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import and_, func, literal, select, union_all
from sqlalchemy.orm import Session

from knowledge.evidence import EvidenceCard, MagicDomain
from knowledge.governance import (
    ClaimEligibility,
    SensitiveInformationLevel,
    StoragePermission,
)
from knowledge.models import KnowledgeEntity
from knowledge.projections import (
    IngestionReceipt,
    KnowledgeNodeVersion,
    KnowledgeRelationshipAssertion,
    ProjectionBuilder,
    QdrantProjection,
    StorageManifest,
    StorageManifestService,
)
from persistence.audit import AuditEventRepository
from persistence.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    IdempotencyRepository,
    canonical_payload_hash,
)
from persistence.models import (
    ClaimCandidate,
    ClaimReviewDecision,
    EvidenceCardVersion,
    GovernanceWorkflowStatus,
    KnowledgeEntityRecord,
    KnowledgeNodeVersionRecord,
    KnowledgeRelationshipAssertionRecord,
    MappingProposalEvidence,
    MappingProposalRecord,
    MappingReviewDecision,
    SessionStatus,
    SourceReviewDecision,
    SourceVersion,
    UserStatus,
)
from persistence.storage_models import (
    ActiveCorpusPointer,
    CorpusActivationState,
    CorpusVersionRecord,
    IngestionOperationRecord,
    IngestionOperationState,
    IngestionReceiptRecord,
    ManifestAuthorizationRecord,
    StorageManifestItemRecord,
    StorageManifestRecord,
    StorageManifestState,
)
from persistence.uow import SqlAlchemyUnitOfWork
from research.bootstrap.semantic import validate_relationship_entailment
from research.extraction.models import ExtractedEntity, RelationshipMappingProposal
from research.extraction.semantic import validate_extracted_entity
from research.review.storage_workflow_models import (
    CorpusActivationCommand,
    CorpusVersionView,
    CorpusVersionsView,
    EligibleArtifactView,
    IngestionResultView,
    ManifestAuthorizationCommand,
    ManifestBuildCommand,
    ManifestIngestionCommand,
    ManifestItemView,
    PersistentProductionWriteCapability,
    STORAGE_VALIDATION_RULE_VERSIONS,
    StorageManifestSummaryView,
    StorageManifestView,
)
from research.review.source_permission_policy import (
    SourcePermissionPolicyError,
    require_current_source_approval,
)
from security.policy import (
    AuthenticatedActor,
    Permission,
    has_permission,
    retrieval_authorization_for_actor,
)
from security.repositories import RoleRepository, SessionRepository, UserRepository


class StorageWorkflowError(RuntimeError):
    status_code = 400

    def __init__(self, code: str, public_message: str) -> None:
        self.code = code
        self.public_message = public_message
        super().__init__(public_message)


class StorageAuthorizationError(StorageWorkflowError):
    status_code = 403

    def __init__(self) -> None:
        super().__init__("forbidden", "The authenticated actor is not authorized.")


class StorageNotFoundError(StorageWorkflowError):
    status_code = 404

    def __init__(self, resource: str) -> None:
        super().__init__(f"{resource}_not_found", "The requested resource was not found.")


class StorageConflictError(StorageWorkflowError):
    status_code = 409


class StorageInputError(StorageWorkflowError):
    status_code = 422


class ProjectionWriterWithCleanup(Protocol):
    def write_manifest(self, manifest: StorageManifest, *, actor: str) -> IngestionReceipt: ...
    def cleanup_manifest_points(self, manifest: StorageManifest) -> str: ...


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    artifact_type: str
    row_id: UUID
    domain_id: UUID
    version: int
    payload_checksum: str
    sensitivity: SensitiveInformationLevel
    projection: QdrantProjection


@dataclass(frozen=True, slots=True)
class ResolvedManifestArtifacts:
    artifacts: tuple[ResolvedArtifact, ...]
    validation_results: dict[str, object]


ArtifactResolver = Callable[
    [Session, ManifestBuildCommand, AuthenticatedActor], ResolvedManifestArtifacts
]
Clock = Callable[[], datetime]

# Discovery is an operator-facing catalogue, not a small review queue.  Keep
# one deterministic SQL snapshot bounded, but large enough for the first
# Production corpus (1,000+ Evidence Cards) and resolve it in batches below.
ELIGIBLE_ARTIFACT_SCAN_LIMIT = 10_000
ELIGIBLE_ARTIFACT_BATCH_SIZE = 128
ELIGIBLE_ARTIFACT_MAX_OFFSET = 9_900
MANIFEST_LIST_SCAN_LIMIT = 250
MANIFEST_LIST_MAX_OFFSET = 100


def utc_now() -> datetime:
    return datetime.now(UTC)


class ProductionStorageWorkflowService:
    """Own manifest, Qdrant saga, and activation transaction boundaries."""

    def __init__(
        self,
        session_factory,
        *,
        writer: ProjectionWriterWithCleanup | None = None,
        artifact_resolver: ArtifactResolver | None = None,
        allow_temporary_qdrant_writes: bool = False,
        persistent_production_write_approved: bool = False,
        persistent_write_capability: PersistentProductionWriteCapability | None = None,
        vector_size: int = 0,
        vector_distance: str = "cosine",
        clock: Clock = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._writer = writer
        self._artifact_resolver = artifact_resolver or self._resolve_approved_artifacts
        self._allow_temporary_writes = allow_temporary_qdrant_writes
        self._persistent_write_approved = persistent_production_write_approved
        self._persistent_write_capability = persistent_write_capability
        self._vector_size = vector_size
        self._vector_distance = vector_distance
        self._clock = clock
        self._manifests = StorageManifestService()

    def close(self) -> None:
        """Release a lazily-created writer client without opening it."""

        close = getattr(self._writer, "close", None)
        if callable(close):
            close()

    def build_manifest(
        self,
        actor: AuthenticatedActor,
        command: ManifestBuildCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> StorageManifestView:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_permission(unit.session, actor, Permission.MANIFEST_BUILD)
            record, replay = self._begin_idempotency(
                unit.session, current, "storage.manifests.build", idempotency_key,
                command.model_dump(mode="json"),
            )
            if replay is not None:
                replay_view = StorageManifestView.model_validate(replay)
                row = unit.session.get(StorageManifestRecord, replay_view.id)
                if row is None:
                    raise StorageConflictError(
                        "idempotency_replay_stale",
                        "The stored manifest replay no longer identifies a valid manifest.",
                    )
                self._require_current_manifest(
                    unit.session,
                    current,
                    row,
                    mismatch_code="manifest_replay_stale",
                )
                current_view = self._manifest_view(unit.session, row.id)
                if current_view != replay_view:
                    raise StorageConflictError(
                        "idempotency_replay_mismatch",
                        "The stored manifest replay does not match current manifest state.",
                    )
                return current_view
            resolved = self._artifact_resolver(unit.session, command, current)
            allowed = set(command.authorized_sensitive_levels)
            actor_allowed = set(
                retrieval_authorization_for_actor(current).allowed_sensitive_levels
            )
            if not allowed.issubset(actor_allowed):
                raise StorageAuthorizationError()
            for artifact in resolved.artifacts:
                if artifact.sensitivity not in allowed:
                    raise StorageConflictError(
                        "manifest_sensitivity_excluded",
                        "An artifact is outside the manifest's authorized sensitivity levels.",
                    )
            manifest = self._manifests.build(
                [artifact.projection for artifact in resolved.artifacts],
                corpus_id=str(command.corpus_id),
                collection_name=command.collection_name,
                created_by=current.username,
                validation_rule_versions=STORAGE_VALIDATION_RULE_VERSIONS,
                authorized_sensitive_levels=command.authorized_sensitive_levels,
            )
            existing = unit.session.get(StorageManifestRecord, UUID(manifest.id))
            if existing is None:
                row = StorageManifestRecord(
                    id=UUID(manifest.id), corpus_id=command.corpus_id,
                    schema_version=manifest.schema_version,
                    projection_schema_version=manifest.projection_schema_version,
                    collection_name=manifest.collection_name,
                    manifest_hash=manifest.manifest_hash,
                    manifest_payload=manifest.model_dump(mode="json"),
                    validation_rule_versions=dict(manifest.validation_rule_versions),
                    authorized_sensitive_levels=[level.value for level in manifest.authorized_sensitive_levels],
                    expected_point_count=manifest.expected_point_count,
                    status=StorageManifestState.PENDING,
                    created_by_user_id=current.user_id,
                    created_at=self._now(), updated_at=self._now(),
                )
                unit.session.add(row)
                # Flush the manifest identity before immutable children.  The
                # models deliberately avoid ORM relationships, so an explicit
                # parent flush keeps SQLite and PostgreSQL ordering identical.
                unit.session.flush()
                for sequence, artifact in enumerate(
                    sorted(resolved.artifacts, key=lambda item: item.projection.knowledge_unit_id), 1
                ):
                    unit.session.add(StorageManifestItemRecord(
                        manifest_id=row.id, sequence_number=sequence,
                        artifact_type=artifact.artifact_type,
                        artifact_row_id=artifact.row_id,
                        artifact_domain_id=artifact.domain_id,
                        artifact_version=artifact.version,
                        payload_checksum=artifact.payload_checksum,
                        projection_checksum=artifact.projection.payload_checksum,
                        projection_point_id=UUID(artifact.projection.knowledge_unit_id),
                        sensitivity=artifact.sensitivity.value,
                        created_at=self._now(),
                    ))
                unit.session.flush()
                self._audit(unit.session).append(
                    event_type="storage_manifest_built", actor_user_id=current.user_id,
                    actor_role_snapshot=roles, object_type="storage_manifest",
                    object_id=manifest.id, reason="Build manifest from exact approved artifact versions.",
                    new_state={"corpus_id": str(command.corpus_id), "manifest_hash": manifest.manifest_hash,
                               "point_count": manifest.expected_point_count, "status": "pending"},
                    request_id=request_id, correlation_id=correlation_id,
                    payload_checksum=manifest.manifest_hash,
                )
            view = self._manifest_view(unit.session, UUID(manifest.id))
            self._complete_idempotency(unit.session, record, view.model_dump(mode="json"), 201)
            unit.commit()
            return view

    def get_manifest(
        self, actor: AuthenticatedActor, manifest_id: UUID
    ) -> StorageManifestView:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_any_permission(
                unit.session, actor,
                {Permission.MANIFEST_BUILD, Permission.MANIFEST_AUTHORIZE, Permission.MANIFEST_INGEST},
            )
            row = unit.session.get(StorageManifestRecord, manifest_id)
            if row is None:
                raise StorageNotFoundError("manifest")
            self._require_current_manifest(
                unit.session,
                current,
                row,
                mismatch_code="manifest_current_approval_mismatch",
            )
            return self._manifest_view(unit.session, manifest_id)

    def list_manifests(
        self,
        actor: AuthenticatedActor,
        *,
        status: str | None = None,
        corpus_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[StorageManifestSummaryView]:
        if (
            not 1 <= limit <= 100
            or offset < 0
            or offset > MANIFEST_LIST_MAX_OFFSET
        ):
            raise StorageInputError(
                "invalid_pagination", "Pagination parameters are invalid."
            )
        try:
            manifest_status = StorageManifestState(status) if status else None
        except ValueError as exc:
            raise StorageInputError(
                "invalid_manifest_status", "The manifest status filter is invalid."
            ) from exc
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_any_permission(
                unit.session,
                actor,
                {
                    Permission.MANIFEST_BUILD,
                    Permission.MANIFEST_AUTHORIZE,
                    Permission.MANIFEST_INGEST,
                },
            )
            statement = select(StorageManifestRecord)
            if manifest_status is not None:
                statement = statement.where(
                    StorageManifestRecord.status == manifest_status
                )
            if corpus_id is not None:
                statement = statement.where(
                    StorageManifestRecord.corpus_id == corpus_id
                )
            rows = list(
                unit.session.scalars(
                    statement.order_by(
                        StorageManifestRecord.created_at.desc(),
                        StorageManifestRecord.id.desc(),
                    )
                    .limit(MANIFEST_LIST_SCAN_LIMIT + 1)
                )
            )
            overflow = len(rows) > MANIFEST_LIST_SCAN_LIMIT
            rows = rows[:MANIFEST_LIST_SCAN_LIMIT]
            summaries: list[StorageManifestSummaryView] = []
            for row in rows:
                try:
                    self._require_current_manifest(
                        unit.session,
                        current,
                        row,
                        mismatch_code="manifest_current_approval_mismatch",
                    )
                except (
                    StorageWorkflowError,
                    ValidationError,
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    # Listing is disclosure-safe: stale, malformed, or
                    # clearance-ineligible manifests are never serialized.
                    continue
                summaries.append(self._manifest_summary(unit.session, row))
                if len(summaries) >= offset + limit:
                    return summaries[offset : offset + limit]
            if overflow:
                raise StorageConflictError(
                    "manifest_scan_limit_exceeded",
                    "The bounded manifest scan could not prove a complete page.",
                )
            return summaries[offset : offset + limit]

    def list_eligible_artifacts(
        self,
        actor: AuthenticatedActor,
        *,
        artifact_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[EligibleArtifactView]:
        """Return only artifacts accepted by the current Manifest resolver."""

        if (
            not 1 <= limit <= 100
            or offset < 0
            or offset > ELIGIBLE_ARTIFACT_MAX_OFFSET
        ):
            raise StorageInputError(
                "invalid_pagination", "Pagination parameters are invalid."
            )
        allowed_types = {"evidence_card", "knowledge_node", "relationship"}
        if artifact_type is not None and artifact_type not in allowed_types:
            raise StorageInputError(
                "invalid_artifact_type", "The artifact type filter is invalid."
            )
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_permission(
                unit.session, actor, Permission.MANIFEST_BUILD
            )
            candidates, overflow = self._bounded_eligible_candidates(
                unit.session, artifact_type=artifact_type
            )
            eligible: list[EligibleArtifactView] = []
            for start in range(0, len(candidates), ELIGIBLE_ARTIFACT_BATCH_SIZE):
                batch = candidates[start : start + ELIGIBLE_ARTIFACT_BATCH_SIZE]
                # Resolve a healthy batch with one complete governance pass.
                # If one candidate poisons the batch, recursively isolate it;
                # valid neighbours remain visible without reverting the common
                # path to one resolver call per row.
                eligible.extend(
                    self._eligible_artifact_views_batch(
                        unit.session, current, batch
                    )
                )
                if len(eligible) >= offset + limit:
                    return eligible[offset : offset + limit]
            if overflow:
                raise StorageConflictError(
                    "eligible_artifact_scan_limit_exceeded",
                    "The bounded eligibility scan could not prove a complete page.",
                )
            return eligible[offset : offset + limit]

    def authorize_manifest(
        self,
        actor: AuthenticatedActor,
        manifest_id: UUID,
        command: ManifestAuthorizationCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> StorageManifestView:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_permission(unit.session, actor, Permission.MANIFEST_AUTHORIZE)
            self._require_named_human(current)
            idem, replay = self._begin_idempotency(
                unit.session, current, f"storage.manifests.{manifest_id}.authorize",
                idempotency_key, {"manifest_id": str(manifest_id), **command.model_dump(mode="json")},
            )
            if replay is not None:
                replay_view = StorageManifestView.model_validate(replay)
                row = unit.session.get(StorageManifestRecord, manifest_id)
                if row is None or replay_view.id != row.id:
                    raise StorageConflictError(
                        "idempotency_replay_stale",
                        "The stored authorization replay no longer identifies a valid manifest.",
                    )
                self._require_current_manifest(
                    unit.session,
                    current,
                    row,
                    mismatch_code="manifest_replay_stale",
                )
                current_view = self._manifest_view(unit.session, row.id)
                if current_view != replay_view:
                    raise StorageConflictError(
                        "idempotency_replay_mismatch",
                        "The stored authorization replay does not match current manifest state.",
                    )
                return current_view
            row = unit.session.scalar(
                select(StorageManifestRecord).where(StorageManifestRecord.id == manifest_id).with_for_update()
            )
            if row is None:
                raise StorageNotFoundError("manifest")
            if row.status != StorageManifestState.PENDING:
                raise StorageConflictError("manifest_not_pending", "Only a pending manifest may be authorized.")
            pending = self._validated_manifest_row(row)
            self._require_manifest_clearance(current, row)
            # Rebuild from the exact rows at authorization time.  This reruns
            # evidence, entity, relationship, sensitivity, and schema gates.
            resolved = self._reconcile_current_manifest(
                unit.session,
                row,
                current,
                pending,
                mismatch_code="manifest_checksum_mismatch",
            )
            authorized = self._manifests.authorize(
                pending, authorizer=current.username, reason=command.reason
            )
            # Preserve trusted account identity and role snapshot in the domain payload.
            authorization = authorized.authorization.model_copy(update={
                "authorizer_user_id": str(current.user_id),
                "actor_role_snapshot": sorted(role.value for role in roles),
            })
            authorized = authorized.model_copy(update={"authorization": authorization})
            row.status = StorageManifestState.AUTHORIZED
            row.manifest_payload = authorized.model_dump(mode="json")
            row.updated_at = self._now()
            unit.session.add(ManifestAuthorizationRecord(
                manifest_id=row.id, manifest_hash=row.manifest_hash,
                authorizer_user_id=current.user_id,
                actor_role_snapshot=sorted(role.value for role in roles),
                reason=command.reason,
                validation_snapshot={
                    "rule_versions": STORAGE_VALIDATION_RULE_VERSIONS,
                    "results": resolved.validation_results,
                    "rebuilt_manifest_hash": pending.manifest_hash,
                },
                authorized_at=self._now(),
            ))
            unit.session.flush()
            self._audit(unit.session).append(
                event_type="storage_manifest_authorized", actor_user_id=current.user_id,
                actor_role_snapshot=roles, object_type="storage_manifest", object_id=str(row.id),
                previous_state={"status": "pending"},
                new_state={"status": "authorized", "manifest_hash": row.manifest_hash,
                           "validation_rule_versions": STORAGE_VALIDATION_RULE_VERSIONS},
                reason=command.reason, request_id=request_id, correlation_id=correlation_id,
                payload_checksum=row.manifest_hash,
            )
            view = self._manifest_view(unit.session, row.id)
            self._complete_idempotency(unit.session, idem, view.model_dump(mode="json"), 200)
            unit.commit()
            return view

    def ingest_manifest(
        self,
        actor: AuthenticatedActor,
        manifest_id: UUID,
        command: ManifestIngestionCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> IngestionResultView:
        operation_id, manifest, current, roles = self._start_ingestion(
            actor, manifest_id, command, idempotency_key=idempotency_key,
            request_id=request_id, correlation_id=correlation_id,
        )
        if isinstance(operation_id, IngestionResultView):
            return operation_id
        assert self._writer is not None
        try:
            receipt = self._writer.write_manifest(manifest, actor=current.username)
            self._validate_receipt(manifest, receipt)
        except Exception as exc:
            cleanup = self._cleanup_after_failed_operation(operation_id, manifest)
            self._finish_failed_ingestion(
                operation_id, current, roles, cleanup=cleanup,
                error_code="qdrant_ingestion_failed",
                request_id=request_id, correlation_id=correlation_id,
            )
            raise StorageConflictError(
                "qdrant_ingestion_failed",
                "The manifest was not ingested; cleanup or reconciliation was recorded.",
            ) from exc
        try:
            return self._finish_successful_ingestion(
                operation_id, current, roles, manifest, receipt,
                reason=command.reason, request_id=request_id, correlation_id=correlation_id,
            )
        except Exception as exc:
            # The vector write completed but the receipt transaction did not.
            # Remove only manifest-owned points; otherwise retain an explicit
            # reconciliation marker.  Never leave the operation as success.
            cleanup = self._cleanup_after_failed_operation(operation_id, manifest)
            try:
                self._finish_failed_ingestion(
                    operation_id, current, roles, cleanup=cleanup,
                    error_code=(
                        exc.code
                        if isinstance(exc, StorageWorkflowError)
                        else "ingestion_finalize_failed"
                    ),
                    request_id=request_id, correlation_id=correlation_id,
                )
            except Exception:
                pass
            raise StorageConflictError(
                "ingestion_finalize_failed",
                "The vector write could not be finalized; cleanup or reconciliation is required.",
            ) from exc

    def activate_corpus(
        self,
        actor: AuthenticatedActor,
        corpus_id: UUID,
        command: CorpusActivationCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> CorpusVersionView:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_permission(unit.session, actor, Permission.CORPUS_ACTIVATE)
            self._require_named_human(current)
            idem, replay = self._begin_idempotency(
                unit.session, current, f"corpora.{corpus_id}.activate", idempotency_key,
                {"corpus_id": str(corpus_id), **command.model_dump(mode="json")},
            )
            if replay is not None:
                return CorpusVersionView.model_validate(replay)
            corpus = unit.session.scalar(
                select(CorpusVersionRecord).where(CorpusVersionRecord.id == corpus_id).with_for_update()
            )
            if corpus is None:
                known_manifest = unit.session.scalar(
                    select(StorageManifestRecord.id).where(
                        StorageManifestRecord.corpus_id == corpus_id
                    ).limit(1)
                )
                if known_manifest is not None:
                    raise StorageConflictError(
                        "corpus_not_ingested",
                        "The corpus was not ingested with a matching receipt and cannot be activated.",
                    )
                raise StorageNotFoundError("corpus")
            receipt = unit.session.get(IngestionReceiptRecord, corpus.ingestion_receipt_id)
            manifest = unit.session.get(StorageManifestRecord, corpus.manifest_id)
            if (
                receipt is None or manifest is None
                or manifest.status != StorageManifestState.INGESTED
                or receipt.manifest_hash != manifest.manifest_hash
                or receipt.corpus_id != corpus.id
            ):
                raise StorageConflictError(
                    "corpus_not_ingested", "Only a receipt-backed ingested corpus may be activated."
                )
            domain_manifest = self._validated_manifest_row(manifest)
            domain_receipt = self._validated_receipt_row(domain_manifest, receipt)
            receipt_operation = unit.session.get(
                IngestionOperationRecord, receipt.operation_id
            )
            if (
                corpus.id != UUID(domain_manifest.corpus_id)
                or corpus.manifest_id != UUID(domain_manifest.id)
                or corpus.ingestion_receipt_id != UUID(domain_receipt.id)
                or corpus.qdrant_collection != domain_manifest.collection_name
                or corpus.schema_version != domain_manifest.schema_version
                or corpus.projection_version
                != domain_manifest.projection_schema_version
                or receipt_operation is None
                or receipt_operation.manifest_id != corpus.manifest_id
                or receipt_operation.state != IngestionOperationState.SUCCEEDED
            ):
                raise StorageConflictError(
                    "corpus_identity_mismatch",
                    "The corpus identity does not match its manifest and receipt.",
                )
            unresolved = unit.session.scalar(
                select(func.count()).select_from(IngestionOperationRecord).where(
                    IngestionOperationRecord.manifest_id == manifest.id,
                    IngestionOperationRecord.state == IngestionOperationState.RECONCILIATION_REQUIRED,
                )
            )
            if unresolved:
                raise StorageConflictError(
                    "corpus_reconciliation_required", "The corpus has unresolved ingestion state."
                )
            pointer = unit.session.scalar(
                select(ActiveCorpusPointer).where(
                    ActiveCorpusPointer.runtime_scope == command.runtime_scope
                ).with_for_update()
            )
            previous_id = pointer.corpus_id if pointer is not None else None
            if previous_id is not None and previous_id != corpus.id:
                previous = unit.session.get(CorpusVersionRecord, previous_id)
                if previous is not None:
                    previous.activation_state = CorpusActivationState.INACTIVE
            if pointer is None:
                pointer = ActiveCorpusPointer(
                    runtime_scope=command.runtime_scope, corpus_id=corpus.id,
                    activated_by_user_id=current.user_id, activated_at=self._now(),
                )
                unit.session.add(pointer)
            else:
                pointer.corpus_id = corpus.id
                pointer.activated_by_user_id = current.user_id
                pointer.activated_at = self._now()
            corpus.runtime_scope = command.runtime_scope
            corpus.activation_state = CorpusActivationState.ACTIVE
            corpus.activated_at = self._now()
            unit.session.flush()
            self._audit(unit.session).append(
                event_type="production_corpus_activated", actor_user_id=current.user_id,
                actor_role_snapshot=roles, object_type="corpus_version", object_id=str(corpus.id),
                previous_state={"active_corpus_id": str(previous_id) if previous_id else None},
                new_state={"active_corpus_id": str(corpus.id), "runtime_scope": command.runtime_scope},
                reason=command.reason, request_id=request_id, correlation_id=correlation_id,
            )
            view = self._corpus_view(corpus)
            self._complete_idempotency(unit.session, idem, view.model_dump(mode="json"), 200)
            unit.commit()
            return view

    def get_active_corpus(
        self, actor: AuthenticatedActor, *, runtime_scope: str = "production"
    ) -> CorpusVersionView:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            self._require_any_permission(
                unit.session, actor,
                {Permission.PRODUCT_READ, Permission.CORPUS_ACTIVATE, Permission.MANIFEST_INGEST},
            )
            pointer = unit.session.get(ActiveCorpusPointer, runtime_scope)
            if pointer is None:
                raise StorageNotFoundError("active_corpus")
            corpus = unit.session.get(CorpusVersionRecord, pointer.corpus_id)
            if corpus is None or corpus.activation_state != CorpusActivationState.ACTIVE:
                raise StorageConflictError("active_corpus_invalid", "The active corpus pointer is invalid.")
            return self._corpus_view(corpus)

    def list_corpora(self, actor: AuthenticatedActor) -> CorpusVersionsView:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            self._require_any_permission(
                unit.session, actor,
                {Permission.PRODUCT_READ, Permission.CORPUS_ACTIVATE, Permission.MANIFEST_INGEST},
            )
            rows = list(unit.session.scalars(
                select(CorpusVersionRecord).order_by(
                    CorpusVersionRecord.created_at.desc(), CorpusVersionRecord.id.desc()
                )
            ))
            return CorpusVersionsView(items=[self._corpus_view(row) for row in rows])

    # --- ingestion saga -------------------------------------------------

    def _start_ingestion(self, actor, manifest_id, command, *, idempotency_key,
                         request_id, correlation_id):
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_permission(unit.session, actor, Permission.MANIFEST_INGEST)
            self._require_named_human(current)
            row = unit.session.scalar(
                select(StorageManifestRecord).where(StorageManifestRecord.id == manifest_id).with_for_update()
            )
            if row is None:
                raise StorageNotFoundError("manifest")
            manifest = self._validated_manifest_row(row)
            self._require_manifest_clearance(current, row)
            # Authorization and write may be separated by a human confirmation
            # window.  Re-run every current approval and rebuild the exact
            # manifest before even recording an ingestion operation.
            self._reconcile_current_manifest(
                unit.session,
                row,
                current,
                manifest,
                mismatch_code="manifest_current_approval_mismatch",
            )
            # Idempotency is evaluated before status/replay handling.  A
            # completed manifest must not turn reuse of the same key with a
            # changed request into an apparent successful replay.
            request_payload = {
                "manifest_id": str(manifest_id),
                "manifest_hash": manifest.manifest_hash,
                **command.model_dump(mode="json"),
            }
            request_hash = canonical_payload_hash(request_payload)
            existing = unit.session.scalar(
                select(IngestionOperationRecord).where(
                    IngestionOperationRecord.actor_user_id == current.user_id,
                    IngestionOperationRecord.idempotency_key == idempotency_key,
                ).with_for_update()
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise StorageConflictError("idempotency_conflict", "The idempotency key was reused.")
                if existing.manifest_id != row.id:
                    raise StorageConflictError(
                        "idempotency_conflict", "The idempotency key was reused."
                    )
                if existing.state == IngestionOperationState.SUCCEEDED:
                    if row.status != StorageManifestState.INGESTED:
                        raise StorageConflictError(
                            "ingestion_state_mismatch",
                            "The successful operation does not match manifest state.",
                        )
                    receipt_row = unit.session.scalar(
                        select(IngestionReceiptRecord).where(
                            IngestionReceiptRecord.operation_id == existing.id
                        )
                    )
                    if receipt_row is None:
                        raise StorageConflictError(
                            "ingestion_receipt_missing",
                            "The completed ingestion operation has no receipt.",
                        )
                    self._validated_receipt_row(manifest, receipt_row)
                    return self._ingestion_view(existing, receipt_row), manifest, current, roles
                raise StorageConflictError(
                    "ingestion_not_retryable", "This ingestion operation is running or requires operator resolution."
                )
            if row.status != StorageManifestState.AUTHORIZED:
                receipt_row = unit.session.scalar(
                    select(IngestionReceiptRecord).where(IngestionReceiptRecord.manifest_id == row.id)
                )
                if row.status == StorageManifestState.INGESTED and receipt_row is not None:
                    operation = unit.session.get(IngestionOperationRecord, receipt_row.operation_id)
                    if operation is None or operation.state != IngestionOperationState.SUCCEEDED:
                        raise StorageConflictError(
                            "ingestion_operation_invalid",
                            "The ingested manifest has no successful operation.",
                        )
                    self._validated_receipt_row(manifest, receipt_row)
                    # Bind a new replay key even though no vector write is
                    # needed.  Reuse of that key with changed request content
                    # will therefore fail rather than bypass idempotency just
                    # because the manifest is already ingested.
                    idem, replay = self._begin_idempotency(
                        unit.session,
                        current,
                        f"storage.manifests.{manifest_id}.ingested-replay",
                        idempotency_key,
                        request_payload,
                    )
                    view = self._ingestion_view(operation, receipt_row)
                    if replay is not None:
                        replay_view = IngestionResultView.model_validate(replay)
                        if replay_view != view:
                            raise StorageConflictError(
                                "idempotency_replay_mismatch",
                                "The stored ingestion replay does not match the active receipt.",
                            )
                    else:
                        self._complete_idempotency(
                            unit.session, idem, view.model_dump(mode="json"), 200
                        )
                        unit.commit()
                    return view, manifest, current, roles
                raise StorageConflictError("manifest_not_authorized", "Only an authorized manifest may be ingested.")
            if self._writer is None:
                raise StorageConflictError("qdrant_writer_unavailable", "Production Qdrant writing is unavailable.")
            temporary = row.collection_name.startswith("magicforge_cp4_test_")
            if temporary and not self._allow_temporary_writes:
                raise StorageConflictError("qdrant_write_disabled", "Temporary Qdrant writing is disabled.")
            if not temporary:
                self._require_persistent_write_capability(row, manifest)
            active_operation = unit.session.scalar(
                select(IngestionOperationRecord).where(
                    IngestionOperationRecord.manifest_id == row.id,
                    IngestionOperationRecord.state.in_((
                        IngestionOperationState.RUNNING,
                        IngestionOperationState.RECONCILIATION_REQUIRED,
                    )),
                ).with_for_update()
            )
            if active_operation is not None:
                raise StorageConflictError(
                    "manifest_ingestion_active",
                    "This manifest already has an active ingestion or requires reconciliation.",
                )
            operation = IngestionOperationRecord(
                manifest_id=row.id, actor_user_id=current.user_id,
                idempotency_key=idempotency_key, request_hash=request_hash,
                state=IngestionOperationState.RUNNING, started_at=self._now(),
            )
            unit.session.add(operation)
            unit.session.flush()
            self._audit(unit.session).append(
                event_type="manifest_ingestion_started", actor_user_id=current.user_id,
                actor_role_snapshot=roles, object_type="ingestion_operation", object_id=str(operation.id),
                new_state={"manifest_id": str(row.id), "collection": row.collection_name,
                           "state": "running"}, reason=command.reason,
                request_id=request_id, correlation_id=correlation_id,
            )
            operation_id = operation.id
            unit.commit()
            return operation_id, manifest, current, roles

    def _cleanup_after_failed_operation(self, operation_id, manifest):
        """Clean only while this operation remains the sole active owner.

        The database partial unique index prevents another active operation;
        this preflight additionally refuses cleanup after a receipt/success has
        appeared, avoiding deletion of points already committed by another
        worker in the event of an unexpected process race.
        """

        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            operation = unit.session.scalar(
                select(IngestionOperationRecord).where(
                    IngestionOperationRecord.id == operation_id
                ).with_for_update()
            )
            manifest_row = unit.session.scalar(
                select(StorageManifestRecord).where(
                    StorageManifestRecord.id == UUID(manifest.id)
                ).with_for_update()
            )
            receipt_exists = unit.session.scalar(
                select(func.count()).select_from(IngestionReceiptRecord).where(
                    IngestionReceiptRecord.manifest_id == UUID(manifest.id)
                )
            )
            competing = unit.session.scalar(
                select(func.count()).select_from(IngestionOperationRecord).where(
                    IngestionOperationRecord.manifest_id == UUID(manifest.id),
                    IngestionOperationRecord.id != operation_id,
                    IngestionOperationRecord.state.in_((
                        IngestionOperationState.RUNNING,
                        IngestionOperationState.RECONCILIATION_REQUIRED,
                        IngestionOperationState.SUCCEEDED,
                    )),
                )
            )
            safe = bool(
                operation is not None
                and operation.manifest_id == UUID(manifest.id)
                and operation.state == IngestionOperationState.RUNNING
                and manifest_row is not None
                and manifest_row.status == StorageManifestState.AUTHORIZED
                and not receipt_exists
                and not competing
            )
        if not safe:
            return "reconciliation_required"
        try:
            return self._writer.cleanup_manifest_points(manifest)
        except Exception:
            return "reconciliation_required"

    def _finish_failed_ingestion(self, operation_id, actor, roles, *, cleanup, error_code,
                                 request_id, correlation_id):
        safe_cleanup = cleanup if cleanup in {"not_needed", "cleaned"} else "reconciliation_required"
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            operation = unit.session.scalar(
                select(IngestionOperationRecord).where(IngestionOperationRecord.id == operation_id).with_for_update()
            )
            if operation is None or operation.state != IngestionOperationState.RUNNING:
                raise StorageConflictError("ingestion_operation_invalid", "The ingestion operation is invalid.")
            operation.state = (
                IngestionOperationState.FAILED_CLEANED
                if safe_cleanup in {"not_needed", "cleaned"}
                else IngestionOperationState.RECONCILIATION_REQUIRED
            )
            operation.cleanup_outcome = safe_cleanup
            operation.error_code = error_code
            operation.finished_at = self._now()
            self._audit(unit.session).append(
                event_type="manifest_ingestion_failed", actor_user_id=actor.user_id,
                actor_role_snapshot=roles, object_type="ingestion_operation", object_id=str(operation.id),
                new_state={
                    "state": operation.state.value,
                    "cleanup_outcome": safe_cleanup,
                    "error_code": error_code,
                },
                reason="Qdrant ingestion failed; safe cleanup outcome was recorded.",
                request_id=request_id, correlation_id=correlation_id,
            )
            unit.commit()

    def _finish_successful_ingestion(self, operation_id, actor, roles, manifest, receipt, *, reason,
                                     request_id, correlation_id):
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, current_roles = self._require_permission(
                unit.session, actor, Permission.MANIFEST_INGEST
            )
            self._require_named_human(current)
            operation = unit.session.scalar(
                select(IngestionOperationRecord).where(IngestionOperationRecord.id == operation_id).with_for_update()
            )
            row = unit.session.scalar(
                select(StorageManifestRecord).where(StorageManifestRecord.id == UUID(manifest.id)).with_for_update()
            )
            if operation is None or row is None or operation.state != IngestionOperationState.RUNNING:
                raise StorageConflictError("ingestion_operation_invalid", "The ingestion operation is invalid.")
            if row.status != StorageManifestState.AUTHORIZED or row.manifest_hash != receipt.manifest_hash:
                raise StorageConflictError("manifest_changed", "The manifest changed during ingestion.")
            # The external write is outside SQL.  Reconcile again under the
            # finalization locks so a revocation, supersession, role/clearance
            # change, or artifact hash change during the write cannot acquire
            # a durable receipt/corpus record.
            self._lock_manifest_governance_envelopes(unit.session, row)
            current_manifest = self._require_current_manifest(
                unit.session,
                current,
                row,
                mismatch_code="manifest_changed_during_ingestion",
            )
            if (
                current_manifest.id != manifest.id
                or current_manifest.manifest_hash != manifest.manifest_hash
            ):
                raise StorageConflictError(
                    "manifest_changed_during_ingestion",
                    "The manifest changed while the vector write was running.",
                )
            if not row.collection_name.startswith("magicforge_cp4_test_"):
                self._require_persistent_write_capability(row, current_manifest)
            self._validate_receipt(current_manifest, receipt)
            receipt_payload = receipt.model_dump(mode="json")
            receipt_row = IngestionReceiptRecord(
                id=UUID(receipt.id), manifest_id=row.id, corpus_id=row.corpus_id,
                manifest_hash=row.manifest_hash, collection_name=row.collection_name,
                receipt_payload=receipt_payload,
                payload_checksum=canonical_payload_hash(receipt_payload), operation_id=operation.id,
                created_at=self._now(),
            )
            unit.session.add(receipt_row)
            # CorpusVersion has a strict receipt FK and no ORM relationship;
            # make the append-only receipt durable in this transaction first.
            unit.session.flush()
            ingested = self._manifests.mark_ingested(manifest, receipt)
            row.status = StorageManifestState.INGESTED
            row.manifest_payload = ingested.model_dump(mode="json")
            row.updated_at = self._now()
            operation.state = IngestionOperationState.SUCCEEDED
            operation.finished_at = self._now()
            corpus = CorpusVersionRecord(
                id=row.corpus_id, manifest_id=row.id, ingestion_receipt_id=receipt_row.id,
                runtime_scope="production", qdrant_collection=row.collection_name,
                schema_version=row.schema_version,
                projection_version=row.projection_schema_version,
                vector_size=self._vector_size, vector_distance=self._vector_distance,
                activation_state=CorpusActivationState.STAGED,
                created_by_user_id=current.user_id, created_at=self._now(),
            )
            unit.session.add(corpus)
            unit.session.flush()
            self._audit(unit.session).append(
                event_type="manifest_ingestion_succeeded", actor_user_id=current.user_id,
                actor_role_snapshot=current_roles, object_type="storage_manifest", object_id=str(row.id),
                previous_state={"status": "authorized"},
                new_state={"status": "ingested", "receipt_id": str(receipt_row.id),
                           "corpus_id": str(row.corpus_id), "point_count": len(receipt.point_ids)},
                reason=reason, request_id=request_id, correlation_id=correlation_id,
                payload_checksum=row.manifest_hash,
            )
            view = self._ingestion_view(operation, receipt_row)
            unit.commit()
            return view

    # --- production artifact discovery/resolver ------------------------

    @staticmethod
    def _bounded_eligible_candidates(session, *, artifact_type):
        """Load a deterministic, strictly bounded cross-artifact candidate set.

        Only the newest immutable version in each artifact family enters the
        catalogue snapshot.  This SQL prefilter prevents superseded history
        from consuming the scan budget.  Eligibility itself remains fail-closed
        and may still discard a candidate after current-policy reconciliation.
        If the bounded snapshot cannot prove a complete logical page, the
        caller returns an explicit error instead of a short/hidden page.
        """

        branches = []
        if artifact_type in {None, "evidence_card"}:
            latest_evidence = (
                select(
                    EvidenceCardVersion.evidence_card_id.label("family_id"),
                    func.max(EvidenceCardVersion.version_number).label(
                        "latest_version"
                    ),
                )
                .group_by(EvidenceCardVersion.evidence_card_id)
                .subquery()
            )
            branches.append(
                select(
                    literal("evidence_card").label("artifact_type"),
                    EvidenceCardVersion.id.label("row_id"),
                    EvidenceCardVersion.created_at.label("created_at"),
                )
                .join(
                    latest_evidence,
                    and_(
                        latest_evidence.c.family_id
                        == EvidenceCardVersion.evidence_card_id,
                        latest_evidence.c.latest_version
                        == EvidenceCardVersion.version_number,
                    ),
                )
            )
        if artifact_type in {None, "knowledge_node"}:
            latest_nodes = (
                select(
                    KnowledgeNodeVersionRecord.entity_id.label("family_id"),
                    func.max(KnowledgeNodeVersionRecord.version_number).label(
                        "latest_version"
                    ),
                )
                .group_by(KnowledgeNodeVersionRecord.entity_id)
                .subquery()
            )
            branches.append(
                select(
                    literal("knowledge_node").label("artifact_type"),
                    KnowledgeNodeVersionRecord.id.label("row_id"),
                    KnowledgeNodeVersionRecord.created_at.label("created_at"),
                )
                .join(
                    latest_nodes,
                    and_(
                        latest_nodes.c.family_id
                        == KnowledgeNodeVersionRecord.entity_id,
                        latest_nodes.c.latest_version
                        == KnowledgeNodeVersionRecord.version_number,
                    ),
                )
            )
        if artifact_type in {None, "relationship"}:
            latest_relationships = (
                select(
                    KnowledgeRelationshipAssertionRecord.relationship_id.label(
                        "family_id"
                    ),
                    func.max(
                        KnowledgeRelationshipAssertionRecord.version_number
                    ).label("latest_version"),
                )
                .group_by(
                    KnowledgeRelationshipAssertionRecord.relationship_id
                )
                .subquery()
            )
            branches.append(
                select(
                    literal("relationship").label("artifact_type"),
                    KnowledgeRelationshipAssertionRecord.id.label("row_id"),
                    KnowledgeRelationshipAssertionRecord.created_at.label("created_at"),
                )
                .join(
                    latest_relationships,
                    and_(
                        latest_relationships.c.family_id
                        == KnowledgeRelationshipAssertionRecord.relationship_id,
                        latest_relationships.c.latest_version
                        == KnowledgeRelationshipAssertionRecord.version_number,
                    ),
                )
            )
        candidate_union = union_all(*branches).subquery()
        references = list(
            session.execute(
                select(
                    candidate_union.c.artifact_type,
                    candidate_union.c.row_id,
                    candidate_union.c.created_at,
                )
                .order_by(
                    candidate_union.c.created_at.desc(),
                    candidate_union.c.row_id.desc(),
                    candidate_union.c.artifact_type.desc(),
                )
                .limit(ELIGIBLE_ARTIFACT_SCAN_LIMIT + 1)
            )
        )
        overflow = len(references) > ELIGIBLE_ARTIFACT_SCAN_LIMIT
        references = references[:ELIGIBLE_ARTIFACT_SCAN_LIMIT]

        models = {
            "evidence_card": EvidenceCardVersion,
            "knowledge_node": KnowledgeNodeVersionRecord,
            "relationship": KnowledgeRelationshipAssertionRecord,
        }
        rows_by_key: dict[tuple[str, UUID], object] = {}
        for kind, model in models.items():
            ids = [reference.row_id for reference in references if reference.artifact_type == kind]
            if not ids:
                continue
            rows_by_key.update(
                {
                    (kind, row.id): row
                    for row in session.scalars(
                        select(model).where(model.id.in_(ids))
                    )
                }
            )
        candidates = [
            (reference.artifact_type, rows_by_key[(reference.artifact_type, reference.row_id)])
            for reference in references
            if (reference.artifact_type, reference.row_id) in rows_by_key
        ]
        return candidates, overflow

    def _eligible_artifact_views_batch(self, session, actor, candidates):
        """Resolve one catalogue batch while isolating invalid members safely.

        The normal path performs one resolver invocation for up to 128 rows.
        A mixed valid/invalid batch is split only after a fail-closed resolver
        error.  The recursive split preserves deterministic candidate order and
        prevents one revoked row from hiding unrelated approved rows.
        """

        if not candidates:
            return []
        try:
            evidence_ids: list[UUID] = []
            node_ids: list[UUID] = []
            relationship_ids: list[UUID] = []
            supporting_by_key: dict[tuple[str, UUID], list[EvidenceCardVersion]] = {}
            subjects: dict[tuple[str, UUID], str] = {}
            support_domains: dict[tuple[str, UUID], list[UUID]] = {}
            proposal_by_key: dict[tuple[str, UUID], UUID] = {}
            for kind, row in candidates:
                key = (kind, row.id)
                if kind == "evidence_card":
                    card = EvidenceCard.model_validate(row.payload)
                    evidence_ids.append(row.id)
                    supporting_by_key[key] = [row]
                    subjects[key] = card.claim
                elif kind == "knowledge_node":
                    node = KnowledgeNodeVersion.model_validate(row.payload)
                    node_ids.append(row.id)
                    proposal_by_key[key] = row.mapping_proposal_id
                    support_domains[key] = [
                        UUID(str(value)) for value in node.supporting_evidence_ids
                    ]
                    subjects[key] = node.entity.name
                elif kind == "relationship":
                    assertion = KnowledgeRelationshipAssertion.model_validate(
                        row.payload
                    )
                    relationship_ids.append(row.id)
                    proposal_by_key[key] = row.mapping_proposal_id
                    support_domains[key] = [
                        UUID(str(value))
                        for value in assertion.supporting_evidence_ids
                    ]
                    subjects[key] = assertion.assertion
                else:
                    raise StorageInputError(
                        "invalid_artifact_type",
                        "The artifact type filter is invalid.",
                    )

            # Batch-load exact MappingProposalEvidence links and their immutable
            # Evidence rows.  This avoids one supporting-Evidence query per node
            # or relationship and also makes the catalogue preview use the same
            # exact version/checksum binding as Manifest construction.
            proposal_ids = set(proposal_by_key.values())
            links = list(
                session.scalars(
                    select(MappingProposalEvidence).where(
                        MappingProposalEvidence.mapping_proposal_id.in_(
                            proposal_ids
                        )
                    )
                )
            ) if proposal_ids else []
            links_by_proposal: dict[UUID, dict[UUID, MappingProposalEvidence]] = {}
            for link in links:
                links_by_proposal.setdefault(link.mapping_proposal_id, {})[
                    link.evidence_domain_object_id
                ] = link
                evidence_ids.append(link.evidence_card_version_id)
            evidence_ids = list(dict.fromkeys(evidence_ids))
            evidence_rows = list(
                session.scalars(
                    select(EvidenceCardVersion).where(
                        EvidenceCardVersion.id.in_(evidence_ids)
                    )
                )
            ) if evidence_ids else []
            evidence_by_id = {row.id: row for row in evidence_rows}
            for key, domains in support_domains.items():
                proposal_links = links_by_proposal.get(proposal_by_key[key], {})
                supporting_by_key[key] = [
                    evidence_by_id[
                        proposal_links[domain_id].evidence_card_version_id
                    ]
                    for domain_id in domains
                ]

            command = ManifestBuildCommand(
                corpus_id=UUID(int=0),
                collection_name="magicforge_eligibility_preview",
                evidence_version_ids=list(dict.fromkeys(evidence_ids)),
                knowledge_node_version_ids=node_ids,
                relationship_assertion_ids=relationship_ids,
                authorized_sensitive_levels=list(
                    retrieval_authorization_for_actor(
                        actor
                    ).allowed_sensitive_levels
                ),
            )
            resolved = self._artifact_resolver(session, command, actor)
            targets = {
                (artifact.artifact_type, artifact.row_id): artifact
                for artifact in resolved.artifacts
            }
            views: list[EligibleArtifactView] = []
            for kind, row in candidates:
                key = (kind, row.id)
                target = targets.get(key)
                if target is None:
                    raise StorageConflictError(
                        "eligible_artifact_resolution_missing",
                        "The artifact was not resolved by the current storage policy.",
                    )
                views.append(
                    EligibleArtifactView(
                        artifact_type=kind,
                        artifact_row_id=target.row_id,
                        artifact_domain_id=target.domain_id,
                        artifact_version=target.version,
                        payload_checksum=target.payload_checksum,
                        sensitivity=target.sensitivity,
                        subject=" ".join(subjects[key].split()),
                        supporting_evidence_version_ids=[
                            item.id for item in supporting_by_key[key]
                        ],
                        approved_at=row.created_at,
                    )
                )
            return views
        except (
            StorageWorkflowError,
            ValidationError,
            KeyError,
            TypeError,
            ValueError,
        ):
            if len(candidates) == 1:
                return []
            midpoint = len(candidates) // 2
            return [
                *self._eligible_artifact_views_batch(
                    session, actor, candidates[:midpoint]
                ),
                *self._eligible_artifact_views_batch(
                    session, actor, candidates[midpoint:]
                ),
            ]

    @staticmethod
    def _supporting_evidence_version_rows(session, domain_ids):
        ordered_ids = [UUID(str(value)) for value in domain_ids]
        if not ordered_ids:
            raise StorageConflictError(
                "supporting_evidence_missing",
                "Every knowledge artifact must include supporting Evidence.",
            )
        rows = list(
            session.scalars(
                select(EvidenceCardVersion).where(
                    EvidenceCardVersion.domain_object_id.in_(ordered_ids)
                )
            )
        )
        by_domain = {row.domain_object_id: row for row in rows}
        try:
            return [by_domain[item_id] for item_id in ordered_ids]
        except KeyError as exc:
            raise StorageConflictError(
                "supporting_evidence_missing",
                "Every knowledge artifact must include its exact Evidence versions.",
            ) from exc

    def _resolve_approved_artifacts(self, session, command, actor):
        authorization = retrieval_authorization_for_actor(actor)
        allowed_actor_levels = set(authorization.allowed_sensitive_levels)
        cards_by_domain: dict[str, EvidenceCard] = {}
        evidence_rows_by_domain: dict[str, EvidenceCardVersion] = {}
        artifacts: list[ResolvedArtifact] = []
        builder = ProjectionBuilder()

        evidence_rows = self._exact_rows(session, EvidenceCardVersion, command.evidence_version_ids, "evidence_version")
        for row in evidence_rows:
            card = self._approved_evidence_card(session, row)
            if card.review.sensitive_information_level not in allowed_actor_levels:
                raise StorageAuthorizationError()
            cards_by_domain[card.id] = card
            evidence_rows_by_domain[card.id] = row
            projection = self._apply_projection_storage_policy(
                builder.from_evidence_card(card), [card]
            )
            artifacts.append(ResolvedArtifact(
                "evidence_card", row.id, row.domain_object_id, row.version_number,
                row.payload_checksum, card.review.sensitive_information_level,
                projection,
            ))

        node_rows = self._exact_rows(session, KnowledgeNodeVersionRecord,
                                     command.knowledge_node_version_ids, "knowledge_node_version")
        for row in node_rows:
            node = self._approved_node(session, row)
            canonical = self._canonical_entity(session, UUID(node.entity.id))
            if (
                row.entity_id != UUID(node.entity.id)
                or canonical.type != node.entity.type
                or canonical.name != node.entity.name
            ):
                raise StorageConflictError(
                    "canonical_entity_identity_mismatch",
                    "The Knowledge Node no longer matches its active canonical entity.",
                )
            supporting = self._supporting_cards(node.supporting_evidence_ids, cards_by_domain)
            self._validate_mapping_evidence_binding(
                session,
                row.mapping_proposal_id,
                node.supporting_evidence_ids,
                evidence_rows_by_domain,
            )
            extracted = ExtractedEntity(
                type=node.entity.type, name=node.entity.name, description=node.definition,
                evidence_excerpt=supporting[0].evidence_excerpt,
                supporting_claims=[card.claim for card in supporting],
                confidence=node.confidence.score,
            )
            decision = validate_extracted_entity(extracted)
            if not decision.accepted:
                raise StorageConflictError("entity_validation_failed", decision.reason)
            sensitivity = max(
                (card.review.sensitive_information_level for card in supporting),
                key=_sensitivity_rank,
            )
            projection = self._apply_projection_storage_policy(
                builder.from_knowledge_node(node, supporting_cards=supporting),
                supporting,
            )
            artifacts.append(ResolvedArtifact(
                "knowledge_node", row.id, UUID(node.id), row.version_number,
                row.payload_checksum, sensitivity,
                projection,
            ))

        assertion_rows = self._exact_rows(
            session, KnowledgeRelationshipAssertionRecord,
            command.relationship_assertion_ids, "relationship_assertion",
        )
        for row in assertion_rows:
            assertion = self._approved_assertion(session, row)
            supporting = self._supporting_cards(assertion.supporting_evidence_ids, cards_by_domain)
            self._validate_mapping_evidence_binding(
                session,
                row.mapping_proposal_id,
                assertion.supporting_evidence_ids,
                evidence_rows_by_domain,
            )
            source = self._canonical_entity(session, UUID(assertion.relationship.source_id))
            target = self._canonical_entity(session, UUID(assertion.relationship.target_id))
            proposal = RelationshipMappingProposal(
                source_entity_id=source.id, target_entity_id=target.id,
                type=assertion.relationship.type, assertion=assertion.assertion,
                evidence_excerpt=supporting[0].evidence_excerpt,
                source_locator=supporting[0].locator.source_locator,
                extraction_confidence=assertion.confidence.score,
                supporting_evidence_card_ids=assertion.supporting_evidence_ids,
            )
            decision = validate_relationship_entailment(proposal, source, target, supporting)
            if not decision.accepted:
                raise StorageConflictError("relationship_entailment_failed", decision.reason)
            context = row.projection_context
            try:
                domains = [MagicDomain(value) for value in context["domains"]]
                paths = [str(value) for value in context["ontology_paths"]]
            except (KeyError, TypeError, ValueError) as exc:
                raise StorageConflictError(
                    "relationship_projection_context_invalid",
                    "The approved relationship projection context is invalid.",
                ) from exc
            sensitivity = max(
                (card.review.sensitive_information_level for card in supporting),
                key=_sensitivity_rank,
            )
            projection = self._apply_projection_storage_policy(
                builder.from_relationship(
                    assertion, source_entity=source, target_entity=target,
                    supporting_cards=supporting, domains=domains, ontology_paths=paths,
                    topic_tags=[str(value) for value in context.get("topic_tags", [])],
                ),
                supporting,
            )
            artifacts.append(ResolvedArtifact(
                "relationship", row.id, UUID(assertion.id), row.version_number,
                row.payload_checksum, sensitivity,
                projection,
            ))
        return ResolvedManifestArtifacts(
            artifacts=tuple(artifacts),
            validation_results={
                "passed": True,
                "artifact_count": len(artifacts),
                "evidence_count": len(evidence_rows),
                "knowledge_node_count": len(node_rows),
                "relationship_count": len(assertion_rows),
                "governance_binding_limitations": [
                    "EvidenceCardVersion has no SourceReviewDecision foreign key; "
                    "the latest Source decision was reconciled against every "
                    "immutable Evidence policy field and rejected on mismatch."
                ],
            },
        )

    def _approved_evidence_card(self, session, row):
        self._verify_payload_checksum(row.payload, row.payload_checksum, "evidence")
        try:
            card = EvidenceCard.model_validate(row.payload)
        except ValidationError as exc:
            raise StorageConflictError("evidence_schema_invalid", "Evidence schema validation failed.") from exc
        if not card.projection_eligible or UUID(card.id) != row.domain_object_id or card.version != row.version_number:
            raise StorageConflictError("evidence_not_eligible", "The exact Evidence Card is not storage-eligible.")
        candidate = session.get(ClaimCandidate, row.claim_candidate_id)
        decision = self._latest_claim_decision(session, row.claim_candidate_id)
        source_decision = self._latest_source_decision(session, row.source_version_id)
        latest_version = session.scalar(
            select(EvidenceCardVersion).where(EvidenceCardVersion.evidence_card_id == row.evidence_card_id)
            .order_by(EvidenceCardVersion.version_number.desc()).limit(1)
        )
        if (
            candidate is None or candidate.status != GovernanceWorkflowStatus.APPROVED
            or decision is None or decision.resulting_status != GovernanceWorkflowStatus.APPROVED
            or decision.id != row.claim_review_decision_id
            or source_decision is None or source_decision.resulting_status != GovernanceWorkflowStatus.APPROVED
            or latest_version is None or latest_version.id != row.id
        ):
            raise StorageConflictError("evidence_approval_stale", "Evidence approval is revoked or superseded.")
        source_version = session.get(SourceVersion, row.source_version_id)
        if source_version is None:
            raise StorageConflictError(
                "source_version_missing",
                "The exact Source version supporting Evidence is unavailable.",
            )
        try:
            require_current_source_approval(
                session,
                source_version,
                source_decision,
            )
        except SourcePermissionPolicyError as exc:
            raise StorageConflictError(exc.code, str(exc)) from exc
        self._validate_current_evidence_policy(card, row, decision, source_decision)
        return card

    def _approved_node(self, session, row):
        self._verify_payload_checksum(row.payload, row.payload_checksum, "knowledge_node")
        try:
            node = KnowledgeNodeVersion.model_validate(row.payload)
        except ValidationError as exc:
            raise StorageConflictError("knowledge_node_schema_invalid", "Knowledge Node schema validation failed.") from exc
        proposal = session.get(MappingProposalRecord, row.mapping_proposal_id)
        review = self._latest_mapping_decision(session, row.mapping_proposal_id)
        latest = session.scalar(
            select(KnowledgeNodeVersionRecord).where(KnowledgeNodeVersionRecord.entity_id == row.entity_id)
            .order_by(KnowledgeNodeVersionRecord.version_number.desc()).limit(1)
        )
        if (
            proposal is None or proposal.status != GovernanceWorkflowStatus.APPROVED
            or proposal.approved_artifact_id != UUID(node.id)
            or proposal.approved_artifact_version != node.version
            or review is None or review.resulting_status != GovernanceWorkflowStatus.APPROVED
            or review.id != row.mapping_review_decision_id
            or latest is None or latest.id != row.id
        ):
            raise StorageConflictError("knowledge_node_approval_stale", "Knowledge Node approval is stale.")
        self._verify_payload_checksum(
            proposal.payload, proposal.proposal_checksum, "mapping_proposal"
        )
        return node

    def _approved_assertion(self, session, row):
        self._verify_payload_checksum(row.payload, row.payload_checksum, "relationship")
        try:
            assertion = KnowledgeRelationshipAssertion.model_validate(row.payload)
        except ValidationError as exc:
            raise StorageConflictError("relationship_schema_invalid", "Relationship schema validation failed.") from exc
        proposal = session.get(MappingProposalRecord, row.mapping_proposal_id)
        review = self._latest_mapping_decision(session, row.mapping_proposal_id)
        latest = session.scalar(
            select(KnowledgeRelationshipAssertionRecord).where(
                KnowledgeRelationshipAssertionRecord.relationship_id == row.relationship_id
            ).order_by(KnowledgeRelationshipAssertionRecord.version_number.desc()).limit(1)
        )
        if (
            proposal is None or proposal.status != GovernanceWorkflowStatus.APPROVED
            or proposal.approved_artifact_id != UUID(assertion.id)
            or proposal.approved_artifact_version != assertion.version
            or review is None or review.resulting_status != GovernanceWorkflowStatus.APPROVED
            or review.id != row.mapping_review_decision_id
            or latest is None or latest.id != row.id
        ):
            raise StorageConflictError("relationship_approval_stale", "Relationship approval is stale.")
        self._verify_payload_checksum(
            proposal.payload, proposal.proposal_checksum, "mapping_proposal"
        )
        return assertion

    def _validate_mapping_evidence_binding(
        self,
        session,
        proposal_id,
        supporting_domain_ids,
        evidence_rows_by_domain,
    ):
        """Require the approved Mapping's exact immutable Evidence snapshot.

        Domain IDs alone are insufficient: a caller must not substitute a
        sibling Evidence version from the same family after Mapping approval.
        The proposal payload, link table, artifact payload, and currently
        resolved Evidence rows must all identify the same exact rows/checksums.
        """

        proposal = session.get(MappingProposalRecord, proposal_id)
        if proposal is None:
            raise StorageConflictError(
                "mapping_evidence_binding_missing",
                "The approved Mapping has no Evidence binding.",
            )
        try:
            proposal_version_ids = {
                UUID(str(value))
                for value in proposal.payload["supporting_evidence_version_ids"]
            }
            artifact_domain_ids = {
                UUID(str(value)) for value in supporting_domain_ids
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise StorageConflictError(
                "mapping_evidence_binding_invalid",
                "The approved Mapping Evidence binding is invalid.",
            ) from exc
        links = list(
            session.scalars(
                select(MappingProposalEvidence).where(
                    MappingProposalEvidence.mapping_proposal_id == proposal_id
                )
            )
        )
        if (
            not links
            or {link.evidence_card_version_id for link in links}
            != proposal_version_ids
            or {link.evidence_domain_object_id for link in links}
            != artifact_domain_ids
        ):
            raise StorageConflictError(
                "mapping_evidence_binding_changed",
                "The approved Mapping no longer has its exact Evidence-version binding.",
            )
        rows_by_id = {row.id: row for row in evidence_rows_by_domain.values()}
        for link in links:
            evidence = rows_by_id.get(link.evidence_card_version_id)
            if (
                evidence is None
                or evidence.domain_object_id != link.evidence_domain_object_id
                or evidence.payload_checksum != link.evidence_payload_checksum
            ):
                raise StorageConflictError(
                    "mapping_evidence_snapshot_changed",
                    "The approved Mapping Evidence snapshot no longer matches its exact version.",
                )

    # --- helpers --------------------------------------------------------

    def _command_from_items(self, session, row):
        items = list(session.scalars(
            select(StorageManifestItemRecord).where(StorageManifestItemRecord.manifest_id == row.id)
            .order_by(StorageManifestItemRecord.sequence_number)
        ))
        groups = {"evidence_card": [], "knowledge_node": [], "relationship": []}
        for item in items:
            if item.artifact_type not in groups:
                raise StorageConflictError(
                    "manifest_item_invalid",
                    "The stored manifest contains an invalid artifact type.",
                )
            groups[item.artifact_type].append(item.artifact_row_id)
        return ManifestBuildCommand(
            corpus_id=row.corpus_id, collection_name=row.collection_name,
            evidence_version_ids=groups["evidence_card"],
            knowledge_node_version_ids=groups["knowledge_node"],
            relationship_assertion_ids=groups["relationship"],
            authorized_sensitive_levels=[SensitiveInformationLevel(value) for value in row.authorized_sensitive_levels],
        )

    @staticmethod
    def _require_manifest_clearance(actor, row):
        try:
            manifest_levels = {
                SensitiveInformationLevel(value)
                for value in row.authorized_sensitive_levels
            }
        except (TypeError, ValueError) as exc:
            raise StorageConflictError(
                "manifest_sensitivity_invalid",
                "The manifest sensitivity envelope is invalid.",
            ) from exc
        actor_levels = set(
            retrieval_authorization_for_actor(actor).allowed_sensitive_levels
        )
        if not manifest_levels.issubset(actor_levels):
            raise StorageAuthorizationError()

    def _require_current_manifest(
        self,
        session,
        actor,
        row,
        *,
        mismatch_code,
    ):
        manifest = self._validated_manifest_row(row)
        self._require_manifest_clearance(actor, row)
        self._reconcile_current_manifest(
            session,
            row,
            actor,
            manifest,
            mismatch_code=mismatch_code,
        )
        return manifest

    def _require_persistent_write_capability(self, row, manifest):
        if not self._persistent_write_approved:
            raise StorageConflictError(
                "persistent_write_not_approved",
                "A separate explicit approval is required for persistent Production Qdrant writes.",
            )
        capability = self._persistent_write_capability
        actual = PersistentProductionWriteCapability(
            manifest_id=row.id,
            manifest_hash=manifest.manifest_hash,
            collection_name=manifest.collection_name,
            point_count=manifest.expected_point_count,
        )
        if capability is None or capability != actual:
            raise StorageConflictError(
                "persistent_write_fingerprint_mismatch",
                "The persistent write capability does not match this exact manifest.",
            )

    @staticmethod
    def _lock_manifest_governance_envelopes(session, manifest_row):
        """Serialize final reconciliation with governed approval mutations.

        Production review services lock SourceVersion, ClaimCandidate, and
        MappingProposalRecord before appending a new decision.  Locking those
        same envelopes here closes the final SQL reconciliation window without
        holding any database lock during the external vector write.
        """

        items = list(
            session.scalars(
                select(StorageManifestItemRecord).where(
                    StorageManifestItemRecord.manifest_id == manifest_row.id
                )
            )
        )
        evidence_ids = {
            item.artifact_row_id
            for item in items
            if item.artifact_type == "evidence_card"
        }
        node_ids = {
            item.artifact_row_id
            for item in items
            if item.artifact_type == "knowledge_node"
        }
        relationship_ids = {
            item.artifact_row_id
            for item in items
            if item.artifact_type == "relationship"
        }
        nodes = list(
            session.scalars(
                select(KnowledgeNodeVersionRecord)
                .where(KnowledgeNodeVersionRecord.id.in_(node_ids))
                .order_by(KnowledgeNodeVersionRecord.id)
                .with_for_update()
            )
        ) if node_ids else []
        relationships = list(
            session.scalars(
                select(KnowledgeRelationshipAssertionRecord)
                .where(KnowledgeRelationshipAssertionRecord.id.in_(relationship_ids))
                .order_by(KnowledgeRelationshipAssertionRecord.id)
                .with_for_update()
            )
        ) if relationship_ids else []
        proposal_ids = {
            row.mapping_proposal_id for row in [*nodes, *relationships]
        }
        if proposal_ids:
            list(
                session.scalars(
                    select(MappingProposalRecord)
                    .where(MappingProposalRecord.id.in_(proposal_ids))
                    .order_by(MappingProposalRecord.id)
                    .with_for_update()
                )
            )
            links = list(
                session.scalars(
                    select(MappingProposalEvidence).where(
                        MappingProposalEvidence.mapping_proposal_id.in_(proposal_ids)
                    )
                )
            )
            evidence_ids.update(link.evidence_card_version_id for link in links)
        evidence_rows = list(
            session.scalars(
                select(EvidenceCardVersion)
                .where(EvidenceCardVersion.id.in_(evidence_ids))
                .order_by(EvidenceCardVersion.id)
                .with_for_update()
            )
        ) if evidence_ids else []
        claim_ids = {row.claim_candidate_id for row in evidence_rows}
        source_version_ids = {row.source_version_id for row in evidence_rows}
        if claim_ids:
            list(
                session.scalars(
                    select(ClaimCandidate)
                    .where(ClaimCandidate.id.in_(claim_ids))
                    .order_by(ClaimCandidate.id)
                    .with_for_update()
                )
            )
        if source_version_ids:
            list(
                session.scalars(
                    select(SourceVersion)
                    .where(SourceVersion.id.in_(source_version_ids))
                    .order_by(SourceVersion.id)
                    .with_for_update()
                )
            )
        entity_ids = {row.entity_id for row in nodes}
        for row in relationships:
            try:
                assertion = KnowledgeRelationshipAssertion.model_validate(row.payload)
                entity_ids.update(
                    {
                        UUID(assertion.relationship.source_id),
                        UUID(assertion.relationship.target_id),
                    }
                )
            except (ValidationError, TypeError, ValueError):
                # The subsequent canonical reconciliation emits the stable
                # schema error; malformed payloads are never accepted.
                continue
        if entity_ids:
            list(
                session.scalars(
                    select(KnowledgeEntityRecord)
                    .where(KnowledgeEntityRecord.id.in_(entity_ids))
                    .order_by(KnowledgeEntityRecord.id)
                    .with_for_update()
                )
            )

    def _reconcile_current_manifest(
        self,
        session,
        row,
        actor,
        stored_manifest,
        *,
        mismatch_code,
    ):
        build_command = self._command_from_items(session, row)
        resolved = self._artifact_resolver(session, build_command, actor)
        stored_items = list(
            session.scalars(
                select(StorageManifestItemRecord)
                .where(StorageManifestItemRecord.manifest_id == row.id)
                .order_by(StorageManifestItemRecord.sequence_number)
            )
        )
        resolved_by_key = {
            (artifact.artifact_type, artifact.row_id): artifact
            for artifact in resolved.artifacts
        }
        if len(stored_items) != len(resolved_by_key):
            raise StorageConflictError(
                mismatch_code,
                "The manifest item envelope no longer matches its approved artifacts.",
            )
        for item in stored_items:
            artifact = resolved_by_key.get(
                (item.artifact_type, item.artifact_row_id)
            )
            if (
                artifact is None
                or item.artifact_domain_id != artifact.domain_id
                or item.artifact_version != artifact.version
                or item.payload_checksum != artifact.payload_checksum
                or item.projection_checksum
                != artifact.projection.payload_checksum
                or item.projection_point_id
                != UUID(artifact.projection.knowledge_unit_id)
                or item.sensitivity != artifact.sensitivity.value
            ):
                raise StorageConflictError(
                    mismatch_code,
                    "The manifest item envelope no longer matches its approved artifacts.",
                )
        rebuilt = self._manifests.build(
            [item.projection for item in resolved.artifacts],
            corpus_id=str(row.corpus_id),
            collection_name=row.collection_name,
            created_by=stored_manifest.created_by,
            validation_rule_versions=STORAGE_VALIDATION_RULE_VERSIONS,
            authorized_sensitive_levels=[
                SensitiveInformationLevel(value)
                for value in row.authorized_sensitive_levels
            ],
        )
        rebuilt_checksums = {
            projection.knowledge_unit_id: projection.payload_checksum
            for projection in rebuilt.projections
        }
        stored_checksums = {
            projection.knowledge_unit_id: projection.payload_checksum
            for projection in stored_manifest.projections
        }
        if (
            rebuilt.id != stored_manifest.id
            or rebuilt.manifest_hash != stored_manifest.manifest_hash
            or rebuilt.expected_point_ids != stored_manifest.expected_point_ids
            or rebuilt_checksums != stored_checksums
        ):
            raise StorageConflictError(
                mismatch_code,
                "The manifest no longer matches the current approved artifact versions.",
            )
        return resolved

    @staticmethod
    def _exact_rows(session, model, ids, resource):
        if not ids:
            return []
        rows = list(session.scalars(select(model).where(model.id.in_(ids))))
        by_id = {row.id: row for row in rows}
        try:
            return [by_id[item_id] for item_id in ids]
        except KeyError as exc:
            raise StorageNotFoundError(resource) from exc

    @staticmethod
    def _supporting_cards(ids, cards):
        try:
            return [cards[item] for item in ids]
        except KeyError as exc:
            raise StorageConflictError(
                "supporting_evidence_missing",
                "Every projected knowledge artifact must include its exact Evidence versions.",
            ) from exc

    @staticmethod
    def _canonical_entity(session, entity_id):
        row = session.get(KnowledgeEntityRecord, entity_id)
        if (
            row is None
            or row.status.value != "active"
            or row.merged_into_entity_id is not None
        ):
            raise StorageConflictError("canonical_entity_inactive", "A relationship endpoint is not active.")
        return KnowledgeEntity(
            id=str(row.id), type=row.entity_type, name=row.canonical_name,
            description=row.description, aliases=row.aliases, attributes=row.attributes,
        )

    @staticmethod
    def _validate_current_evidence_policy(card, row, claim_decision, source_decision):
        """Reconcile the unbound current Source approval using fail-closed policy.

        EvidenceCardVersion predates a direct SourceReviewDecision foreign key.
        Therefore the latest decision is authoritative only when every policy
        dimension is compatible with the immutable card and its Claim decision.
        """

        source_eligibility = source_decision.claim_eligibility
        claim_eligibility = claim_decision.claim_eligibility
        expected_eligibility = min(
            (source_eligibility, claim_eligibility),
            key=_claim_eligibility_rank,
        )
        expected_storage = min(
            (source_decision.storage_permission, claim_decision.storage_permission),
            key=_storage_permission_rank,
        )
        expected_sensitivity = max(
            (
                source_decision.sensitive_information_level,
                claim_decision.sensitive_information_level,
            ),
            key=_sensitivity_rank,
        )
        policy_mismatch = (
            not source_eligibility.allows_projection
            or not claim_eligibility.allows_projection
            or not source_decision.extraction_permission.allows_claim_extraction
            or not source_decision.storage_permission.allows_projection
            or not claim_decision.storage_permission.allows_projection
            or card.review.claim_eligibility != expected_eligibility
            or card.review.extraction_permission != source_decision.extraction_permission
            or card.review.storage_permission != expected_storage
            or card.review.sensitive_information_level != expected_sensitivity
            or card.review.claim_eligibility != claim_decision.claim_eligibility
            or card.knowledge_origin != claim_decision.knowledge_origin
            or card.evidence_class != claim_decision.evidence_class
            or card.evidence_level != claim_decision.evidence_level
            or card.contradiction_status != claim_decision.contradiction_status
            or card.review.contradicting_evidence_checked
            != claim_decision.contradiction_check_status
            or card.secret_exposure_level != claim_decision.secret_exposure_level
            or _sensitivity_rank(card.review.sensitive_information_level)
            < card.secret_exposure_level.rank
            or UUID(card.source.source_version_id) != row.source_version_id
            or card.schema_version != row.domain_schema_version
        )
        if policy_mismatch:
            raise StorageConflictError(
                "evidence_governance_mismatch",
                "Current Source/Claim approval policy is inconsistent with the Evidence Card.",
            )

    @staticmethod
    def _apply_projection_storage_policy(projection, cards):
        expected_sensitivity = max(
            (card.review.sensitive_information_level for card in cards),
            key=_sensitivity_rank,
        )
        expected_exposure = max(
            (card.secret_exposure_level for card in cards),
            key=lambda value: value.rank,
        )
        if (
            projection.sensitive_information_level != expected_sensitivity
            or projection.secret_exposure_level != expected_exposure
            or projection.secret_exposure_rank != expected_exposure.rank
        ):
            raise StorageConflictError(
                "projection_security_mismatch",
                "Projection security metadata is weaker than its supporting Evidence.",
            )
        strictest_storage = min(
            (card.review.storage_permission for card in cards),
            key=_storage_permission_rank,
        )
        if strictest_storage == StoragePermission.DERIVED_KNOWLEDGE_ONLY:
            return QdrantProjection.model_validate(
                {
                    **projection.model_dump(mode="json"),
                    "source_locator": "withheld by storage policy",
                    "page_number": None,
                }
            )
        return projection

    @staticmethod
    def _verify_payload_checksum(payload, expected, resource):
        if canonical_payload_hash(payload) != expected:
            raise StorageConflictError(
                f"{resource}_checksum_mismatch", "An immutable artifact checksum does not match."
            )

    @staticmethod
    def _latest_claim_decision(session, candidate_id):
        return session.scalar(select(ClaimReviewDecision).where(
            ClaimReviewDecision.claim_candidate_id == candidate_id
        ).order_by(ClaimReviewDecision.sequence_number.desc()).limit(1))

    @staticmethod
    def _latest_source_decision(session, source_version_id):
        return session.scalar(select(SourceReviewDecision).where(
            SourceReviewDecision.source_version_id == source_version_id
        ).order_by(SourceReviewDecision.sequence_number.desc()).limit(1))

    @staticmethod
    def _latest_mapping_decision(session, proposal_id):
        return session.scalar(select(MappingReviewDecision).where(
            MappingReviewDecision.mapping_proposal_id == proposal_id
        ).order_by(MappingReviewDecision.sequence_number.desc()).limit(1))

    @staticmethod
    def _validated_manifest_row(row):
        try:
            manifest = StorageManifest.model_validate(row.manifest_payload)
        except ValidationError as exc:
            raise StorageConflictError("manifest_schema_invalid", "The stored manifest is invalid.") from exc
        if (
            manifest.id != str(row.id)
            or manifest.manifest_hash != row.manifest_hash
            or manifest.corpus_id != str(row.corpus_id)
            or manifest.schema_version != row.schema_version
            or manifest.projection_schema_version != row.projection_schema_version
            or manifest.collection_name != row.collection_name
            or manifest.expected_point_count != row.expected_point_count
            or manifest.validation_rule_versions != row.validation_rule_versions
            or [level.value for level in manifest.authorized_sensitive_levels]
            != row.authorized_sensitive_levels
            or manifest.status.value != row.status.value
        ):
            raise StorageConflictError("manifest_checksum_mismatch", "The stored manifest checksum is invalid.")
        return manifest

    @staticmethod
    def _validate_receipt(manifest, receipt):
        expected_checksums = {
            projection.knowledge_unit_id: projection.payload_checksum
            for projection in manifest.projections
        }
        if (
            receipt.manifest_id != manifest.id or receipt.manifest_hash != manifest.manifest_hash
            or receipt.corpus_id != manifest.corpus_id
            or receipt.collection_name != manifest.collection_name
            or receipt.point_ids != manifest.expected_point_ids
            or receipt.payload_checksums != expected_checksums
        ):
            raise StorageConflictError("ingestion_receipt_mismatch", "The ingestion receipt does not match the manifest.")

    @classmethod
    def _validated_receipt_row(cls, manifest, row):
        if canonical_payload_hash(row.receipt_payload) != row.payload_checksum:
            raise StorageConflictError(
                "ingestion_receipt_checksum_mismatch",
                "The persisted ingestion receipt checksum is invalid.",
            )
        try:
            receipt = IngestionReceipt.model_validate(row.receipt_payload)
        except ValidationError as exc:
            raise StorageConflictError(
                "ingestion_receipt_schema_invalid",
                "The persisted ingestion receipt is invalid.",
            ) from exc
        if (
            row.id != UUID(receipt.id)
            or row.manifest_id != UUID(receipt.manifest_id)
            or row.corpus_id != UUID(receipt.corpus_id)
            or row.manifest_hash != receipt.manifest_hash
            or row.collection_name != receipt.collection_name
        ):
            raise StorageConflictError(
                "ingestion_receipt_identity_mismatch",
                "The persisted receipt identity does not match its envelope.",
            )
        cls._validate_receipt(manifest, receipt)
        return receipt

    def _manifest_view(self, session, manifest_id):
        row = session.get(StorageManifestRecord, manifest_id)
        if row is None:
            raise StorageNotFoundError("manifest")
        items = list(session.scalars(select(StorageManifestItemRecord).where(
            StorageManifestItemRecord.manifest_id == row.id
        ).order_by(StorageManifestItemRecord.sequence_number)))
        authorization = session.scalar(select(ManifestAuthorizationRecord).where(
            ManifestAuthorizationRecord.manifest_id == row.id
        ))
        return StorageManifestView(
            id=row.id, corpus_id=row.corpus_id, manifest_hash=row.manifest_hash,
            collection_name=row.collection_name, schema_version=row.schema_version,
            projection_schema_version=row.projection_schema_version,
            status=row.status.value, expected_point_count=row.expected_point_count,
            validation_rule_versions=row.validation_rule_versions,
            authorized_sensitive_levels=[SensitiveInformationLevel(value) for value in row.authorized_sensitive_levels],
            created_by_user_id=row.created_by_user_id, created_at=row.created_at,
            authorized_by_user_id=authorization.authorizer_user_id if authorization else None,
            authorized_at=authorization.authorized_at if authorization else None,
            items=[ManifestItemView(
                artifact_type=item.artifact_type, artifact_row_id=item.artifact_row_id,
                artifact_domain_id=item.artifact_domain_id, artifact_version=item.artifact_version,
                payload_checksum=item.payload_checksum,
                projection_checksum=item.projection_checksum,
                projection_point_id=item.projection_point_id,
                sensitivity=SensitiveInformationLevel(item.sensitivity),
            ) for item in items],
        )

    @staticmethod
    def _manifest_summary(session, row):
        authorization = session.scalar(
            select(ManifestAuthorizationRecord).where(
                ManifestAuthorizationRecord.manifest_id == row.id
            )
        )
        return StorageManifestSummaryView(
            id=row.id,
            corpus_id=row.corpus_id,
            manifest_hash=row.manifest_hash,
            collection_name=row.collection_name,
            schema_version=row.schema_version,
            projection_schema_version=row.projection_schema_version,
            status=row.status.value,
            expected_point_count=row.expected_point_count,
            created_by_user_id=row.created_by_user_id,
            created_at=row.created_at,
            authorized_by_user_id=(
                authorization.authorizer_user_id if authorization else None
            ),
            authorized_at=authorization.authorized_at if authorization else None,
        )

    @staticmethod
    def _ingestion_view(operation, receipt):
        return IngestionResultView(
            operation_id=operation.id, manifest_id=receipt.manifest_id,
            corpus_id=receipt.corpus_id, receipt_id=receipt.id,
            collection_name=receipt.collection_name,
            point_count=len(receipt.receipt_payload["point_ids"]),
        )

    @staticmethod
    def _corpus_view(corpus):
        return CorpusVersionView(
            corpus_id=corpus.id, manifest_id=corpus.manifest_id,
            ingestion_receipt_id=corpus.ingestion_receipt_id,
            runtime_scope=corpus.runtime_scope, qdrant_collection=corpus.qdrant_collection,
            schema_version=corpus.schema_version, projection_version=corpus.projection_version,
            vector_size=corpus.vector_size, vector_distance=corpus.vector_distance,
            activation_state=corpus.activation_state.value,
            created_at=corpus.created_at, activated_at=corpus.activated_at,
        )

    def _require_permission(self, session, actor, permission):
        return self._require_any_permission(session, actor, {permission})

    def _require_any_permission(self, session, actor, permissions):
        if actor.user_id is None or actor.session_id is None or actor.is_bootstrap_anonymous:
            raise StorageAuthorizationError()
        live_session = SessionRepository(session).get(actor.session_id, for_update=True)
        now = self._now()
        if (
            live_session is None or live_session.user_id != actor.user_id
            or live_session.status != SessionStatus.ACTIVE
            or _aware_utc(live_session.expires_at) <= now
            or live_session.transport != actor.session_transport
        ):
            raise StorageAuthorizationError()
        user = UserRepository(session).get(actor.user_id, for_update=True)
        if user is None or user.status != UserStatus.ACTIVE:
            raise StorageAuthorizationError()
        roles = RoleRepository(session).active_names_for_user(actor.user_id)
        current = AuthenticatedActor(
            user_id=user.id, username=user.username, roles=roles,
            session_id=live_session.id, session_transport=live_session.transport,
            session_expires_at=_aware_utc(live_session.expires_at), break_glass=actor.break_glass,
        )
        if not any(has_permission(current, permission) for permission in permissions):
            raise StorageAuthorizationError()
        return current, roles

    @staticmethod
    def _require_named_human(actor):
        if actor.username.casefold() in {"system", "automation", "research-pipeline", "llm", "glm"}:
            raise StorageAuthorizationError()

    def _begin_idempotency(self, session, actor, scope, key, payload):
        try:
            record, replay = IdempotencyRepository(session).begin(
                actor_user_id=actor.user_id, scope=scope, key=key,
                request_hash=canonical_payload_hash(payload), now=self._now(),
            )
        except IdempotencyConflictError as exc:
            raise StorageConflictError("idempotency_conflict", "The idempotency key was reused.") from exc
        except IdempotencyInProgressError as exc:
            raise StorageConflictError("idempotency_in_progress", "The operation is already in progress.") from exc
        return record, replay.body if replay is not None else None

    def _complete_idempotency(self, session, record, body, status_code):
        IdempotencyRepository(session).complete(
            record, status_code=status_code, response_body=body, now=self._now()
        )

    @staticmethod
    def _audit(session):
        return AuditEventRepository(session)

    def _now(self):
        return _aware_utc(self._clock())


def _aware_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sensitivity_rank(value):
    return {
        SensitiveInformationLevel.PUBLIC: 0,
        SensitiveInformationLevel.CONTROLLED: 1,
        SensitiveInformationLevel.SECRET_METHOD: 2,
        SensitiveInformationLevel.RESTRICTED: 3,
    }[value]


def _storage_permission_rank(value):
    return {
        StoragePermission.NONE: 0,
        StoragePermission.DERIVED_KNOWLEDGE_ONLY: 1,
        StoragePermission.DERIVED_WITH_SHORT_EXCERPT: 2,
    }[value]


def _claim_eligibility_rank(value):
    return {
        ClaimEligibility.NOT_ASSESSED: 0,
        ClaimEligibility.INELIGIBLE: 0,
        ClaimEligibility.ELIGIBLE_WITH_LIMITS: 1,
        ClaimEligibility.ELIGIBLE: 2,
    }[value]


__all__ = [
    "ProductionStorageWorkflowService",
    "ResolvedArtifact",
    "ResolvedManifestArtifacts",
    "StorageAuthorizationError",
    "StorageConflictError",
    "StorageInputError",
    "StorageNotFoundError",
    "StorageWorkflowError",
]
