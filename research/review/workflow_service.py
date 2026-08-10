"""Transactional Production Source and Claim governance application service.

Checkpoint 3 intentionally stops at immutable Evidence Card versions.  It does
not build storage manifests, write Qdrant, or activate a Production corpus.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from knowledge.evidence import (
    ClaimRole,
    ConfidenceAssessment,
    ConfidenceLabel,
    ContradictionStatus,
    EvidenceCard,
    EvidenceClass,
    EvidenceReview,
    EvidenceSource,
    KnowledgeOrigin,
    classification_for_evidence_class,
    evidence_excerpt_is_locatable,
)
from knowledge.governance import (
    ClaimEligibility,
    ContradictionCheckStatus,
    ExtractionPermission,
    ReviewStatus,
    SecretExposureLevel,
    SensitiveInformationLevel,
    StoragePermission,
    require_human_identity,
)
from persistence import (
    AuditEventRepository,
    CandidateCreatedByKind,
    CitationVerificationEvidence,
    ClaimCandidate,
    ClaimReviewDecision,
    EvidenceCardVersion,
    GovernanceWorkflowStatus,
    ReviewDecisionKind,
    RoleName,
    SessionStatus,
    Source,
    SourceReviewDecision,
    SourceVersion,
    SqlAlchemyUnitOfWork,
    UserStatus,
)
from persistence.idempotency import (
    IdempotencyConflictError,
    IdempotencyError,
    IdempotencyInProgressError,
    IdempotencyRepository,
    canonical_payload_hash,
)
from persistence.models import SessionTransport, SourcePermissionRequest
from research.citation.models import (
    CitationStatus,
    VerificationMethod,
    VerificationScope,
)
from research.models import ContentAccess, SourceCategory
from research.review.workflow_models import (
    CLAIM_CANDIDATE_SCHEMA_VERSION,
    REVIEW_POLICY_VERSION,
    SOURCE_PERMISSION_REQUEST_SCHEMA_VERSION,
    SOURCE_WORKFLOW_SCHEMA_VERSION,
    CitationMetadataInput,
    CitationVerificationInput,
    CitationVerificationEvidenceView,
    CitationVerificationMethod,
    CitationVerificationScope,
    ClaimCandidateCommand,
    ClaimCandidateReviewView,
    ClaimCandidateSummary,
    ClaimReviewCommand,
    ClaimReviewResult,
    ClaimSourceContextView,
    EvidenceVersionView,
    ResolverResult,
    ReviewDecision,
    SourceReviewCommand,
    SourceReviewDetail,
    SourceReviewQueueItem,
    SourceAccessMetadata,
    SourcePermissionRequestCommand,
    SourcePermissionRequestView,
    SourceSummary,
    SourceVersionCommand,
    SourceVersionReviewView,
    SourceVersionSummary,
    WorkflowStatus,
)
from research.review.source_models import ExtractionScope, SourceApprovalRecord
from research.review.source_permission_policy import (
    SourcePermissionPolicyError,
    permission_request_checksum_payload,
    require_current_source_approval,
    require_permission_request_chain_integrity,
    require_permission_request_integrity,
)
from research.review.claim_models import ClaimReviewItem
from security.policy import (
    AuthenticatedActor,
    Permission,
    has_permission,
    retrieval_authorization_for_actor,
)
from security.repositories import RoleRepository, SessionRepository, UserRepository


class GovernanceServiceError(RuntimeError):
    status_code = 400

    def __init__(self, code: str, public_message: str) -> None:
        self.code = code
        self.public_message = public_message
        super().__init__(public_message)


class GovernanceAuthorizationError(GovernanceServiceError):
    status_code = 403

    def __init__(
        self,
        code: str = "forbidden",
        public_message: str = "The authenticated actor is not authorized.",
    ) -> None:
        super().__init__(code, public_message)


class GovernanceNotFoundError(GovernanceServiceError):
    status_code = 404

    def __init__(self, resource: str) -> None:
        super().__init__(f"{resource}_not_found", "The requested resource was not found.")


class GovernanceConflictError(GovernanceServiceError):
    status_code = 409


class GovernanceInputError(GovernanceServiceError):
    status_code = 422


Clock = Callable[[], datetime]
AuditRepositoryFactory = Callable[[Session], AuditEventRepository]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ProductionGovernanceService:
    """Own all Source/Claim workflow transitions and their transaction boundary."""

    def __init__(
        self,
        session_factory,
        *,
        clock: Clock = utc_now,
        audit_repository_factory: AuditRepositoryFactory = AuditEventRepository,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._audit_factory = audit_repository_factory

    def register_source(
        self,
        actor: AuthenticatedActor,
        command: SourceVersionCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> SourceSummary:
        request = _source_idempotency_payload(command)
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_permission(
                unit.session, actor, Permission.SOURCE_REGISTER
            )
            self._ensure_sensitivity_visible(
                current, command.sensitive_information_level
            )
            record, replay = self._begin_idempotency(
                unit.session,
                current,
                "sources.register",
                idempotency_key,
                request,
            )
            if replay is not None:
                return self._authorized_source_replay(
                    unit.session, current, replay
                )

            canonical_key, identity = _source_identity(command)
            existing = unit.session.scalar(
                select(Source).where(Source.canonical_key == canonical_key)
            )
            if existing is not None:
                raise GovernanceConflictError(
                    "source_already_registered",
                    "This source is already registered; create a new immutable version instead.",
                )
            source = Source(
                id=uuid5(NAMESPACE_URL, f"magicforge:research:{identity}"),
                canonical_key=canonical_key,
                submitted_by_user_id=current.user_id,
                created_at=self._now(),
                updated_at=self._now(),
            )
            unit.session.add(source)
            unit.flush()
            version = self._create_source_version(
                unit.session,
                source,
                command,
                current,
                roles,
                version_number=1,
                supersedes=None,
            )
            permission_request = self._create_initial_permission_request(
                unit.session,
                version,
                command,
                current,
                roles,
            )
            status = self._citation_status(unit.session, version)
            summary = self._source_summary_for_actor(unit.session, source, current)
            self._audit_factory(unit.session).append(
                event_type="source_registered",
                actor_user_id=current.user_id,
                actor_role_snapshot=roles,
                object_type="source",
                object_id=str(source.id),
                new_state={
                    "source_version_id": str(version.id),
                    "version": version.version_number,
                    "content_hash": version.content_hash,
                    "source_permission_request_id": str(permission_request.id),
                    "source_permission_request_checksum": (
                        permission_request.request_checksum
                    ),
                    "citation_status": status.value,
                    "status": WorkflowStatus.SUBMITTED.value,
                },
                reason="Register an immutable Source version for governed review.",
                request_id=request_id,
                correlation_id=correlation_id,
                payload_checksum=version.content_hash,
            )
            body = summary.model_dump(mode="json")
            IdempotencyRepository(unit.session).complete(
                record, status_code=201, response_body=body, now=self._now()
            )
            unit.commit()
            return summary

    def add_source_version(
        self,
        actor: AuthenticatedActor,
        source_id: UUID,
        command: SourceVersionCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> SourceSummary:
        request = {
            "source_id": str(source_id),
            "version": _source_idempotency_payload(command),
        }
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_permission(
                unit.session, actor, Permission.SOURCE_REGISTER
            )
            self._ensure_sensitivity_visible(
                current, command.sensitive_information_level
            )
            record, replay = self._begin_idempotency(
                unit.session,
                current,
                f"sources.{source_id}.versions",
                idempotency_key,
                request,
            )
            if replay is not None:
                return self._authorized_source_replay(
                    unit.session, current, replay
                )
            source = unit.session.scalar(
                select(Source).where(Source.id == source_id).with_for_update()
            )
            if source is None:
                raise GovernanceNotFoundError("source")
            canonical_key, _ = _source_identity(command)
            if canonical_key != source.canonical_key:
                raise GovernanceConflictError(
                    "source_identity_changed",
                    "A Source version cannot change the Source's canonical identity.",
                )
            content_hash = _content_hash(command.content)
            duplicate = unit.session.scalar(
                select(SourceVersion).where(
                    SourceVersion.source_id == source.id,
                    SourceVersion.content_hash == content_hash,
                )
            )
            if duplicate is not None:
                raise GovernanceConflictError(
                    "source_content_already_versioned",
                    "This exact Source content already has an immutable version.",
                )
            latest = unit.session.scalar(
                select(SourceVersion)
                .where(SourceVersion.source_id == source.id)
                .order_by(SourceVersion.version_number.desc())
                .limit(1)
                .with_for_update()
            )
            version_number = 1 if latest is None else latest.version_number + 1
            version = self._create_source_version(
                unit.session,
                source,
                command,
                current,
                roles,
                version_number=version_number,
                supersedes=latest,
            )
            permission_request = self._create_initial_permission_request(
                unit.session,
                version,
                command,
                current,
                roles,
            )
            summary = self._source_summary_for_actor(unit.session, source, current)
            self._audit_factory(unit.session).append(
                event_type="source_version_created",
                actor_user_id=current.user_id,
                actor_role_snapshot=roles,
                object_type="source_version",
                object_id=str(version.id),
                new_state={
                    "source_id": str(source.id),
                    "version": version.version_number,
                    "content_hash": version.content_hash,
                    "source_permission_request_id": str(permission_request.id),
                    "source_permission_request_checksum": (
                        permission_request.request_checksum
                    ),
                    "supersedes_version_id": (
                        str(version.supersedes_version_id)
                        if version.supersedes_version_id
                        else None
                    ),
                    "status": WorkflowStatus.SUBMITTED.value,
                },
                reason="Create a new immutable Source version for governed review.",
                request_id=request_id,
                correlation_id=correlation_id,
                payload_checksum=version.content_hash,
            )
            body = summary.model_dump(mode="json")
            IdempotencyRepository(unit.session).complete(
                record, status_code=201, response_body=body, now=self._now()
            )
            unit.commit()
            return summary

    def get_source(
        self, actor: AuthenticatedActor, source_id: UUID
    ) -> SourceReviewDetail:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_permission(
                unit.session, actor, Permission.SOURCE_REVIEW
            )
            source = unit.session.get(Source, source_id)
            if source is None:
                raise GovernanceNotFoundError("source")
            authorization = retrieval_authorization_for_actor(current)
            detail = self._source_review_detail(
                unit.session,
                source,
                allowed_sensitive_levels=set(authorization.allowed_sensitive_levels),
            )
            if not detail.versions:
                raise GovernanceNotFoundError("source")
            return detail

    def add_source_permission_request(
        self,
        actor: AuthenticatedActor,
        source_id: UUID,
        version_id: UUID,
        command: SourcePermissionRequestCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> SourcePermissionRequestView:
        """Append a new rights/permission ceiling without rewriting Source content."""

        request = {
            "source_id": str(source_id),
            "version_id": str(version_id),
            "permission_request": command.model_dump(mode="json"),
        }
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_permission(
                unit.session, actor, Permission.SOURCE_REGISTER
            )
            if RoleName.OPERATOR not in roles:
                raise GovernanceAuthorizationError(
                    "operator_required",
                    "Only an operator may submit a Source permission request.",
                )
            record, replay = self._begin_idempotency(
                unit.session,
                current,
                f"sources.{source_id}.versions.{version_id}.permission_requests",
                idempotency_key,
                request,
            )
            if replay is not None:
                return self._authorized_permission_request_replay(
                    unit.session, current, source_id, version_id, replay
                )
            if command.source_version_id != version_id:
                raise GovernanceConflictError(
                    "permission_request_version_mismatch",
                    "The permission request does not match the Source version path.",
                )
            version = unit.session.scalar(
                select(SourceVersion)
                .where(
                    SourceVersion.id == version_id,
                    SourceVersion.source_id == source_id,
                )
                .with_for_update()
            )
            if version is None:
                raise GovernanceNotFoundError("source")
            self._ensure_sensitivity_visible(
                current,
                self._effective_source_sensitivity(unit.session, version),
            )
            if self._source_state(unit.session, version.id) != WorkflowStatus.SUBMITTED:
                raise GovernanceConflictError(
                    "source_not_submitted",
                    "Permission requests may only amend a submitted Source version.",
                )
            self._validate_permission_request(version, command)
            latest = self._latest_source_permission_request(
                unit.session, version.id, for_update=True
            )
            if latest is not None:
                self._require_latest_permission_request_integrity(
                    unit.session, latest
                )
            if latest is not None and _permission_request_material_payload(
                command
            ) == self._stored_permission_request_material_payload(latest):
                raise GovernanceConflictError(
                    "permission_request_unchanged",
                    "The new permission request must materially change the current request.",
                )
            sequence = 1 if latest is None else latest.sequence_number + 1
            row = self._create_permission_request(
                unit.session,
                version,
                command,
                current,
                roles,
                sequence_number=sequence,
                supersedes=latest,
            )
            view = self._permission_request_view(unit.session, row)
            self._audit_factory(unit.session).append(
                event_type="source_permission_request_created",
                actor_user_id=current.user_id,
                actor_role_snapshot=roles,
                object_type="source_permission_request",
                object_id=str(row.id),
                previous_state=(
                    {"source_permission_request_id": str(latest.id)}
                    if latest is not None
                    else None
                ),
                new_state={
                    "source_id": str(source_id),
                    "source_version_id": str(version.id),
                    "sequence": row.sequence_number,
                    "requested_extraction_permission": (
                        row.requested_extraction_permission.value
                    ),
                    "requested_storage_permission": (
                        row.requested_storage_permission.value
                    ),
                    "requested_scope_locators": row.requested_scope_locators,
                    "request_checksum": row.request_checksum,
                    "supersedes_request_id": (
                        str(row.supersedes_request_id)
                        if row.supersedes_request_id is not None
                        else None
                    ),
                },
                reason=command.reason,
                request_id=request_id,
                correlation_id=correlation_id,
                payload_checksum=row.request_checksum,
            )
            body = view.model_dump(mode="json")
            IdempotencyRepository(unit.session).complete(
                record, status_code=201, response_body=body, now=self._now()
            )
            unit.commit()
            return view

    def list_sources(
        self,
        actor: AuthenticatedActor,
        *,
        status: WorkflowStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SourceReviewQueueItem]:
        """List visible Source versions, filtering before pagination."""

        if not 1 <= limit <= 100 or offset < 0:
            raise GovernanceInputError(
                "invalid_pagination", "Pagination parameters are invalid."
            )
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_permission(
                unit.session, actor, Permission.SOURCE_REVIEW
            )
            allowed = set(
                retrieval_authorization_for_actor(current).allowed_sensitive_levels
            )
            rows = list(
                unit.session.execute(
                    select(SourceVersion, Source)
                    .join(Source, Source.id == SourceVersion.source_id)
                    .order_by(SourceVersion.created_at.asc(), SourceVersion.id.asc())
                )
            )
            visible: list[SourceReviewQueueItem] = []
            for version, source in rows:
                sensitivity = self._effective_source_sensitivity(unit.session, version)
                if sensitivity not in allowed:
                    continue
                current_status = self._source_state(unit.session, version.id)
                if status is not None and current_status != status:
                    continue
                latest_permission_request = self._latest_source_permission_request(
                    unit.session, version.id
                )
                if latest_permission_request is not None:
                    self._require_latest_permission_request_integrity(
                        unit.session, latest_permission_request
                    )
                visible.append(
                    SourceReviewQueueItem(
                        source_id=source.id,
                        source_version_id=version.id,
                        version=version.version_number,
                        canonical_key=source.canonical_key,
                        title=str(version.citation_metadata.get("title") or ""),
                        source_category=SourceCategory(
                            version.citation_metadata.get(
                                "source_category", SourceCategory.WEB.value
                            )
                        ),
                        source_type=version.source_type,
                        knowledge_origin=version.knowledge_origin,
                        content_access=version.content_access,
                        citation_status=self._citation_status(
                            unit.session, version
                        ).value,
                        status=current_status,
                        latest_permission_request_id=(
                            latest_permission_request.id
                            if latest_permission_request is not None
                            else None
                        ),
                        sensitivity=sensitivity,
                        created_at=version.created_at,
                    )
                )
            return visible[offset : offset + limit]

    def review_source(
        self,
        actor: AuthenticatedActor,
        source_id: UUID,
        command: SourceReviewCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> SourceSummary:
        request = {
            "source_id": str(source_id),
            "review": command.model_dump(mode="json"),
        }
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_permission(
                unit.session, actor, Permission.SOURCE_REVIEW
            )
            self._require_human_reviewer(current)
            record, replay = self._begin_idempotency(
                unit.session,
                current,
                f"sources.{source_id}.review",
                idempotency_key,
                request,
            )
            if replay is not None:
                return self._authorized_source_replay(
                    unit.session, current, replay
                )
            source = unit.session.get(Source, source_id)
            version = unit.session.scalar(
                select(SourceVersion)
                .where(
                    SourceVersion.id == command.source_version_id,
                    SourceVersion.source_id == source_id,
                )
                .with_for_update()
            )
            if source is None or version is None:
                raise GovernanceNotFoundError("source")
            permission_request = self._latest_source_permission_request(
                unit.session, version.id, for_update=True
            )
            if permission_request is None:
                raise GovernanceConflictError(
                    "source_permission_request_required",
                    "The Source version has no governed permission request.",
                )
            self._require_latest_permission_request_integrity(
                unit.session, permission_request
            )
            if command.source_permission_request_id != permission_request.id:
                raise GovernanceConflictError(
                    "stale_source_permission_request",
                    "The Source decision must bind the latest permission request.",
                )
            if version.submitted_by_user_id == current.user_id:
                raise GovernanceConflictError(
                    "self_review_forbidden",
                    "A reviewer cannot review their own Source submission.",
                )
            permission_request_submitters = set(
                unit.session.scalars(
                    select(SourcePermissionRequest.submitted_by_user_id).where(
                        SourcePermissionRequest.source_version_id == version.id
                    )
                )
            )
            if current.user_id in permission_request_submitters:
                raise GovernanceConflictError(
                    "permission_request_self_review_forbidden",
                    "A reviewer cannot decide a Source permission history they submitted to.",
                )
            current_status = self._source_state(unit.session, version.id)
            _require_transition(
                "Source", current_status, _decision_status(command.decision)
            )
            self._ensure_sensitivity_visible(current, version.sensitivity)
            if command.decision == ReviewDecision.APPROVE:
                for verification in command.citation_verification:
                    unit.session.add(
                        self._build_citation_evidence(
                            version,
                            verification,
                            current,
                            roles,
                        )
                    )
                unit.session.flush()
            citation_status, citation_evidence = self._citation_status_with_evidence(
                unit.session, version
            )
            self._validate_source_decision(
                command,
                version,
                permission_request,
                citation_status,
                current,
            )
            sequence = self._next_source_decision_sequence(unit.session, version.id)
            resulting_status = _decision_status(command.decision)
            decision_kind = (
                ReviewDecisionKind.APPROVED
                if command.decision == ReviewDecision.APPROVE
                else ReviewDecisionKind.REJECTED
            )
            decision_payload = {
                "source_version_id": str(version.id),
                "source_permission_request_id": str(permission_request.id),
                "sequence": sequence,
                "decision": decision_kind.value,
                "resulting_status": resulting_status.value,
                "reviewer_user_id": str(current.user_id),
                "reason": command.reason.strip(),
                "citation_status": citation_status.value,
                "citation_verification_evidence_id": (
                    str(citation_evidence.id)
                    if citation_evidence is not None
                    and command.decision == ReviewDecision.APPROVE
                    else None
                ),
                "claim_eligibility": command.claim_eligibility.value,
                "extraction_permission": command.extraction_permission.value,
                "extraction_scope": command.extraction_scope_locators,
                "storage_permission": command.storage_permission.value,
                "sensitivity": command.sensitive_information_level.value,
                "contradiction_check": command.contradicting_evidence_checked.value,
                "policy": REVIEW_POLICY_VERSION,
            }
            checksum = canonical_payload_hash(decision_payload)
            decision = SourceReviewDecision(
                id=uuid5(
                    NAMESPACE_URL,
                    f"magicforge:source-review-decision:{version.id}:{sequence}:{checksum}",
                ),
                source_version_id=version.id,
                source_permission_request_id=permission_request.id,
                sequence_number=sequence,
                decision=decision_kind,
                resulting_status=_orm_status(resulting_status),
                reviewer_user_id=current.user_id,
                actor_role_snapshot=sorted(role.value for role in roles),
                reason=command.reason.strip(),
                citation_verification_evidence_id=(
                    citation_evidence.id
                    if citation_evidence is not None
                    and command.decision == ReviewDecision.APPROVE
                    else None
                ),
                claim_eligibility=command.claim_eligibility,
                extraction_permission=command.extraction_permission,
                storage_permission=command.storage_permission,
                sensitive_information_level=command.sensitive_information_level,
                contradicting_evidence_checked=command.contradicting_evidence_checked,
                extraction_scope={"locators": command.extraction_scope_locators},
                domain_schema_version=REVIEW_POLICY_VERSION,
                decision_checksum=checksum,
                created_at=self._now(),
            )
            unit.session.add(decision)
            unit.flush()
            summary = self._source_summary_for_actor(unit.session, source, current)
            self._audit_factory(unit.session).append(
                event_type=(
                    "source_approved"
                    if resulting_status == WorkflowStatus.APPROVED
                    else "source_rejected"
                ),
                actor_user_id=current.user_id,
                actor_role_snapshot=roles,
                object_type="source_version",
                object_id=str(version.id),
                previous_state={"status": current_status.value},
                new_state={
                    "status": resulting_status.value,
                    "decision_id": str(decision.id),
                    "decision_checksum": checksum,
                    "source_permission_request_id": str(permission_request.id),
                    "citation_status": citation_status.value,
                },
                reason=command.reason.strip(),
                request_id=request_id,
                correlation_id=correlation_id,
                payload_checksum=checksum,
            )
            body = summary.model_dump(mode="json")
            IdempotencyRepository(unit.session).complete(
                record, status_code=200, response_body=body, now=self._now()
            )
            unit.commit()
            return summary

    def submit_claim(
        self,
        actor: AuthenticatedActor,
        command: ClaimCandidateCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ClaimCandidateSummary:
        request = command.model_dump(mode="json")
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_permission(
                unit.session, actor, Permission.CLAIM_SUBMIT
            )
            record, replay = self._begin_idempotency(
                unit.session,
                current,
                "claims.submit",
                idempotency_key,
                request,
            )
            if replay is not None:
                return self._authorized_claim_replay(
                    unit.session, current, replay
                )
            version = unit.session.scalar(
                select(SourceVersion)
                .where(SourceVersion.id == command.source_version_id)
                .with_for_update()
            )
            if version is None:
                raise GovernanceNotFoundError("source_version")
            source_decision = self._approved_source_decision(unit.session, version.id)
            self._ensure_sensitivity_visible(
                current,
                self._effective_source_sensitivity(unit.session, version),
            )
            self._validate_claim_candidate(command, version, source_decision)
            origin, evidence_level = classification_for_evidence_class(
                command.proposed_evidence_class
            )
            proposal = command.model_dump(mode="json")
            proposal["derived_knowledge_origin"] = origin.value
            proposal["derived_evidence_level"] = evidence_level.value
            checksum = canonical_payload_hash(proposal)
            duplicate = unit.session.scalar(
                select(ClaimCandidate).where(
                    ClaimCandidate.source_version_id == version.id,
                    ClaimCandidate.candidate_checksum == checksum,
                )
            )
            if duplicate is not None:
                raise GovernanceConflictError(
                    "claim_candidate_already_submitted",
                    "This exact Claim candidate is already submitted.",
                )
            candidate_id = uuid5(
                NAMESPACE_URL,
                f"magicforge:claim-candidate:{version.id}:{checksum}",
            )
            canonical_claim_id = uuid5(
                NAMESPACE_URL,
                f"magicforge:claim:{command.claim.casefold()}",
            )
            provenance = command.extraction_provenance.model_dump(mode="json")
            model_metadata = {
                key: provenance[key]
                for key in ("run_id", "llm_provider", "model", "tool_version")
                if provenance.get(key) is not None
            }
            candidate = ClaimCandidate(
                id=candidate_id,
                source_version_id=version.id,
                canonical_claim_id=canonical_claim_id,
                claim_text=command.claim,
                claim_role=command.claim_role,
                claim_polarity=command.claim_polarity,
                locator=command.locator.model_dump(mode="json"),
                extraction_provenance=provenance,
                model_run_metadata=model_metadata,
                proposed_evidence_card=proposal,
                candidate_schema_version=CLAIM_CANDIDATE_SCHEMA_VERSION,
                candidate_checksum=checksum,
                extraction_confidence=command.extraction_confidence,
                evidence_card_eligible=command.claim_role != ClaimRole.CONTEXT_ONLY,
                status=GovernanceWorkflowStatus.SUBMITTED,
                submitted_by_user_id=current.user_id,
                created_by_kind=CandidateCreatedByKind(
                    command.extraction_provenance.producer.value
                ),
                created_at=self._now(),
                updated_at=self._now(),
            )
            unit.session.add(candidate)
            unit.flush()
            summary = self._claim_summary(unit.session, candidate)
            self._audit_factory(unit.session).append(
                event_type="claim_submitted",
                actor_user_id=current.user_id,
                actor_role_snapshot=roles,
                object_type="claim_candidate",
                object_id=str(candidate.id),
                new_state={
                    "source_version_id": str(version.id),
                    "claim_role": candidate.claim_role.value,
                    "candidate_checksum": checksum,
                    "status": WorkflowStatus.SUBMITTED.value,
                    "evidence_card_eligible": candidate.evidence_card_eligible,
                },
                reason="Submit a checksummed Claim candidate for human review.",
                request_id=request_id,
                correlation_id=correlation_id,
                payload_checksum=checksum,
            )
            body = summary.model_dump(mode="json")
            IdempotencyRepository(unit.session).complete(
                record, status_code=201, response_body=body, now=self._now()
            )
            unit.commit()
            return summary

    def get_claim(
        self, actor: AuthenticatedActor, claim_id: UUID
    ) -> ClaimCandidateReviewView:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_permission(
                unit.session, actor, Permission.CLAIM_REVIEW
            )
            candidate = unit.session.get(ClaimCandidate, claim_id)
            if candidate is None or not self._claim_visible(unit.session, current, candidate):
                raise GovernanceNotFoundError("claim")
            source_version = unit.session.get(
                SourceVersion, candidate.source_version_id
            )
            allowed = set(
                retrieval_authorization_for_actor(current).allowed_sensitive_levels
            )
            if (
                source_version is None
                or (
                    source_decision := self._latest_source_decision(
                        unit.session, source_version.id
                    )
                )
                is None
                or source_decision.resulting_status
                != GovernanceWorkflowStatus.APPROVED
                or not self._source_approval_is_current(
                    unit.session,
                    source_version,
                    source_decision,
                )
                or self._effective_source_sensitivity(unit.session, source_version)
                not in allowed
            ):
                raise GovernanceNotFoundError("claim")
            return self._claim_review_detail(unit.session, candidate)

    def list_claims(
        self,
        actor: AuthenticatedActor,
        *,
        status: WorkflowStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ClaimCandidateSummary]:
        if not 1 <= limit <= 100 or offset < 0:
            raise GovernanceInputError(
                "invalid_pagination", "Pagination parameters are invalid."
            )
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_permission(
                unit.session, actor, Permission.CLAIM_REVIEW
            )
            allowed = set(
                retrieval_authorization_for_actor(current).allowed_sensitive_levels
            )
            statement = select(ClaimCandidate)
            if status is not None:
                statement = statement.where(
                    ClaimCandidate.status == GovernanceWorkflowStatus(status.value)
                )
            statement = statement.order_by(
                ClaimCandidate.created_at.asc(), ClaimCandidate.id.asc()
            )
            # ``offset`` is defined over the visible queue, not over raw SQL
            # rows.  Scan deterministic, bounded SQL windows until the requested
            # visible page is complete.  This avoids both hidden-row page holes
            # and an unbounded candidate materialization.
            visible_seen = 0
            page: list[ClaimCandidateSummary] = []
            raw_offset = 0
            batch_size = max(100, min(500, limit * 4))
            source_visibility: dict[UUID, bool] = {}
            while len(page) < limit:
                candidates = list(
                    unit.session.scalars(
                        statement.offset(raw_offset).limit(batch_size)
                    )
                )
                if not candidates:
                    break
                raw_offset += len(candidates)
                for candidate in candidates:
                    source_visible = source_visibility.get(
                        candidate.source_version_id
                    )
                    if source_visible is None:
                        source_version = unit.session.get(
                            SourceVersion, candidate.source_version_id
                        )
                        source_decision = (
                            self._latest_source_decision(
                                unit.session, candidate.source_version_id
                            )
                            if source_version is not None
                            else None
                        )
                        source_visible = bool(
                            source_version is not None
                            and source_decision is not None
                            and source_decision.resulting_status
                            == GovernanceWorkflowStatus.APPROVED
                            and self._source_approval_is_current(
                                unit.session,
                                source_version,
                                source_decision,
                            )
                            and self._effective_source_sensitivity(
                                unit.session, source_version
                            )
                            in allowed
                        )
                        source_visibility[candidate.source_version_id] = (
                            source_visible
                        )
                    if not source_visible:
                        continue
                    sensitivity = self._effective_claim_sensitivity(
                        unit.session, candidate
                    )
                    if sensitivity not in allowed:
                        continue
                    if visible_seen >= offset:
                        page.append(
                            self._claim_summary(
                                unit.session,
                                candidate,
                                sensitivity=sensitivity,
                            )
                        )
                    visible_seen += 1
                    if len(page) >= limit:
                        break
                if len(candidates) < batch_size:
                    break
            return page

    def review_claim(
        self,
        actor: AuthenticatedActor,
        claim_id: UUID,
        command: ClaimReviewCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> ClaimReviewResult:
        request = {
            "claim_id": str(claim_id),
            "review": _claim_review_idempotency_payload(command),
        }
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_permission(
                unit.session, actor, Permission.CLAIM_REVIEW
            )
            self._require_human_reviewer(current)
            record, replay = self._begin_idempotency(
                unit.session,
                current,
                f"claims.{claim_id}.review",
                idempotency_key,
                request,
            )
            if replay is not None:
                return self._authorized_claim_review_replay(
                    unit.session, current, replay
                )
            candidate = unit.session.scalar(
                select(ClaimCandidate)
                .where(ClaimCandidate.id == claim_id)
                .with_for_update()
            )
            if candidate is None:
                raise GovernanceNotFoundError("claim")
            if not self._claim_visible(unit.session, current, candidate):
                raise GovernanceNotFoundError("claim")
            if candidate.submitted_by_user_id == current.user_id:
                raise GovernanceConflictError(
                    "self_review_forbidden",
                    "A reviewer cannot review their own Claim submission.",
                )
            current_status = WorkflowStatus(candidate.status.value)
            resulting_status = _decision_status(command.decision)
            _require_transition("Claim", current_status, resulting_status)
            version = unit.session.get(SourceVersion, candidate.source_version_id)
            if version is None:
                raise GovernanceConflictError(
                    "source_version_missing",
                    "The Claim's immutable Source version is unavailable.",
                )
            source_decision = self._approved_source_decision(unit.session, version.id)
            source_sensitivity = self._effective_source_sensitivity(
                unit.session, version
            )
            decision_id = uuid4()
            evidence: EvidenceCard | None = None
            evidence_row: EvidenceCardVersion | None = None
            if command.decision == ReviewDecision.APPROVE:
                self._validate_claim_approval(
                    candidate,
                    command,
                    current,
                    source_decision,
                )
                try:
                    evidence = self._build_evidence_card(
                        unit.session,
                        candidate,
                        version,
                        source_decision,
                        command,
                        current,
                        decision_id,
                    )
                except ValidationError as exc:
                    raise GovernanceInputError(
                        "evidence_card_invalid",
                        "The reviewed Claim does not form a valid Evidence Card.",
                    ) from exc
                evidence_payload = evidence.model_dump(mode="json")
                evidence_checksum = canonical_payload_hash(evidence_payload)
            else:
                evidence_payload = None
                evidence_checksum = None

            proposal = candidate.proposed_evidence_card
            evidence_class = (
                evidence.evidence_class if evidence is not None else None
            )
            knowledge_origin = (
                evidence.knowledge_origin if evidence is not None else None
            )
            evidence_level = evidence.evidence_level if evidence is not None else None
            sequence = self._next_claim_decision_sequence(unit.session, candidate.id)
            decision_kind = (
                ReviewDecisionKind.APPROVED
                if command.decision == ReviewDecision.APPROVE
                else ReviewDecisionKind.REJECTED
            )
            confidence_payload = (
                evidence.confidence.model_dump(mode="json")
                if evidence is not None and evidence.confidence is not None
                else None
            )
            limitations = evidence.limitations if evidence is not None else command.limitations
            decision_payload = {
                "claim_candidate_id": str(candidate.id),
                "sequence": sequence,
                "decision": decision_kind.value,
                "resulting_status": resulting_status.value,
                "reviewer_user_id": str(current.user_id),
                "reason": command.reason.strip(),
                "confidence": confidence_payload,
                "limitations": limitations,
                "evidence_class": evidence_class.value if evidence_class else None,
                "knowledge_origin": knowledge_origin.value if knowledge_origin else None,
                "evidence_level": evidence_level.value if evidence_level else None,
                "claim_eligibility": command.claim_eligibility.value,
                "storage_permission": command.storage_permission.value,
                "contradiction_status": command.contradiction_status.value,
                "contradiction_check": command.contradicting_evidence_checked.value,
                "contradicting_evidence_ids": [
                    str(value) for value in command.contradicting_evidence_ids
                ],
                "sensitivity": command.sensitive_information_level.value,
                "secret_exposure": command.secret_exposure_level.value,
                "policy": REVIEW_POLICY_VERSION,
                "evidence_payload_checksum": evidence_checksum,
            }
            decision_checksum = canonical_payload_hash(decision_payload)
            decision = ClaimReviewDecision(
                id=decision_id,
                claim_candidate_id=candidate.id,
                sequence_number=sequence,
                decision=decision_kind,
                resulting_status=_orm_status(resulting_status),
                reviewer_user_id=current.user_id,
                actor_role_snapshot=sorted(role.value for role in roles),
                reason=command.reason.strip(),
                confidence=confidence_payload,
                limitations=limitations,
                evidence_class=evidence_class,
                knowledge_origin=knowledge_origin,
                evidence_level=evidence_level,
                claim_eligibility=(
                    command.claim_eligibility
                    if evidence is not None
                    else ClaimEligibility.INELIGIBLE
                ),
                storage_permission=(
                    command.storage_permission
                    if evidence is not None
                    else StoragePermission.NONE
                ),
                contradiction_status=(
                    command.contradiction_status
                    if evidence is not None
                    else ContradictionStatus.NOT_CHECKED
                ),
                contradiction_check_status=(
                    command.contradicting_evidence_checked
                    if evidence is not None
                    else ContradictionCheckStatus.NOT_CHECKED
                ),
                contradicting_evidence_ids=[
                    str(value) for value in command.contradicting_evidence_ids
                ],
                sensitive_information_level=(
                    command.sensitive_information_level
                    if evidence is not None
                    else source_sensitivity
                ),
                secret_exposure_level=command.secret_exposure_level,
                policy_schema_version=REVIEW_POLICY_VERSION,
                decision_checksum=decision_checksum,
                created_at=self._now(),
            )
            unit.session.add(decision)
            unit.flush()

            if evidence is not None and evidence_payload is not None:
                evidence_family_id = candidate.id
                evidence_row = EvidenceCardVersion(
                    id=uuid5(
                        NAMESPACE_URL,
                        f"magicforge:evidence-version-row:{evidence.id}",
                    ),
                    evidence_card_id=evidence_family_id,
                    domain_object_id=UUID(evidence.id),
                    version_number=evidence.version,
                    claim_candidate_id=candidate.id,
                    claim_review_decision_id=decision.id,
                    source_id=version.source_id,
                    source_version_id=version.id,
                    citation_id=version.citation_id,
                    canonical_claim_id=UUID(evidence.canonical_claim_id),
                    domain_schema_version=evidence.schema_version,
                    payload=evidence_payload,
                    payload_checksum=evidence_checksum,
                    created_by_user_id=current.user_id,
                    created_at=self._now(),
                )
                unit.session.add(evidence_row)
                unit.flush()

            candidate.status = _orm_status(resulting_status)
            candidate.updated_at = self._now()
            unit.flush()
            summary = self._claim_summary(unit.session, candidate)
            result = ClaimReviewResult(
                claim=summary,
                evidence_card_id=(
                    evidence_row.evidence_card_id if evidence_row is not None else None
                ),
                evidence_version_id=(evidence_row.id if evidence_row is not None else None),
            )
            self._audit_factory(unit.session).append(
                event_type=(
                    "claim_approved"
                    if resulting_status == WorkflowStatus.APPROVED
                    else "claim_rejected"
                ),
                actor_user_id=current.user_id,
                actor_role_snapshot=roles,
                object_type="claim_candidate",
                object_id=str(candidate.id),
                previous_state={"status": current_status.value},
                new_state={
                    "status": resulting_status.value,
                    "decision_id": str(decision.id),
                    "decision_checksum": decision_checksum,
                    "evidence_card_id": (
                        str(evidence_row.evidence_card_id)
                        if evidence_row is not None
                        else None
                    ),
                    "evidence_version_id": (
                        str(evidence_row.id) if evidence_row is not None else None
                    ),
                },
                reason=command.reason.strip(),
                request_id=request_id,
                correlation_id=correlation_id,
                payload_checksum=decision_checksum,
            )
            body = result.model_dump(mode="json")
            IdempotencyRepository(unit.session).complete(
                record, status_code=200, response_body=body, now=self._now()
            )
            unit.commit()
            return result

    def list_evidence_versions(
        self,
        actor: AuthenticatedActor,
        identifier: UUID,
    ) -> list[EvidenceVersionView]:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_permission(
                unit.session, actor, Permission.PRODUCT_READ
            )
            authorization = retrieval_authorization_for_actor(current)
            exact_row = unit.session.get(EvidenceCardVersion, identifier)
            if exact_row is not None:
                # Mapping proposals carry exact SQL EvidenceCardVersion row IDs.
                # An exact-row lookup must never broaden into family history.
                rows = [exact_row]
            else:
                rows = list(
                    unit.session.scalars(
                        select(EvidenceCardVersion)
                        .where(
                            or_(
                                EvidenceCardVersion.evidence_card_id == identifier,
                                EvidenceCardVersion.domain_object_id == identifier,
                            )
                        )
                        .order_by(EvidenceCardVersion.version_number.asc())
                    )
                )
            views: list[EvidenceVersionView] = []
            for row in rows:
                current_card = self._current_evidence_card(unit.session, row)
                if current_card is None:
                    continue
                card, sensitivity = current_card
                if sensitivity not in authorization.allowed_sensitive_levels:
                    continue
                if card.secret_exposure_level.rank > int(authorization.clearance_rank):
                    continue
                if not card.projection_eligible:
                    continue
                expose_excerpt = (
                    card.review.storage_permission
                    == StoragePermission.DERIVED_WITH_SHORT_EXCERPT
                )
                assert card.confidence is not None
                views.append(
                    EvidenceVersionView(
                        evidence_card_id=row.evidence_card_id,
                        evidence_version_id=row.id,
                        version=row.version_number,
                        schema_version=card.schema_version,
                        claim=card.claim,
                        claim_role=card.claim_role,
                        evidence_class=card.evidence_class,
                        knowledge_origin=card.knowledge_origin,
                        applicable_domain=card.applicable_domain,
                        ontology_paths=card.ontology_paths,
                        source_type=card.source.source_type,
                        source_year=card.source.source_year,
                        citation_id=UUID(card.source.citation_id),
                        source_locator=(
                            card.locator.source_locator if expose_excerpt else None
                        ),
                        evidence_excerpt=(
                            card.evidence_excerpt if expose_excerpt else None
                        ),
                        limitations=card.limitations,
                        confidence_score=card.confidence.score,
                        confidence_label=card.confidence.label.value,
                        contradiction_status=card.contradiction_status,
                        sensitivity=sensitivity,
                        secret_exposure_level=card.secret_exposure_level,
                        created_at=row.created_at,
                    )
                )
            if not views:
                raise GovernanceNotFoundError("evidence")
            return views

    def _create_source_version(
        self,
        session: Session,
        source: Source,
        command: SourceVersionCommand,
        actor: AuthenticatedActor,
        roles: frozenset[RoleName],
        *,
        version_number: int,
        supersedes: SourceVersion | None,
    ) -> SourceVersion:
        assert actor.user_id is not None
        content_hash = _content_hash(command.content)
        version_id = uuid5(
            NAMESPACE_URL,
            f"magicforge:source-version:{source.id}:{content_hash}",
        )
        citation_identity = command.citation.doi or _canonical_url(command.citation.url)
        citation_id = uuid5(
            NAMESPACE_URL, f"magicforge:citation:{citation_identity}"
        )
        citation = command.citation.model_dump(mode="json")
        citation["source_category"] = command.source_category.value
        access = command.access.model_dump(mode="json")
        access.update(
            {
                "requested_extraction_permission": (
                    command.requested_extraction_permission.value
                ),
                "requested_storage_permission": command.requested_storage_permission.value,
                "requested_scope_locators": command.requested_scope_locators,
            }
        )
        version = SourceVersion(
            id=version_id,
            source_id=source.id,
            version_number=version_number,
            citation_id=citation_id,
            domain_schema_version=SOURCE_WORKFLOW_SCHEMA_VERSION,
            citation_metadata=citation,
            access_metadata=access,
            content_text=command.content,
            content_hash=content_hash,
            content_access=command.content_access,
            source_type=command.source_type,
            knowledge_origin=command.knowledge_origin,
            sensitivity=command.sensitive_information_level,
            submitted_by_user_id=actor.user_id,
            supersedes_version_id=supersedes.id if supersedes else None,
            created_at=self._now(),
        )
        session.add(version)
        session.flush()
        for verification in command.citation_verification:
            session.add(
                self._build_citation_evidence(
                    version,
                    verification,
                    actor,
                    roles,
                )
            )
        session.flush()
        return version

    def _create_initial_permission_request(
        self,
        session: Session,
        version: SourceVersion,
        source_command: SourceVersionCommand,
        actor: AuthenticatedActor,
        roles: frozenset[RoleName],
    ) -> SourcePermissionRequest:
        """Capture registration permissions as request 1 without any elevation."""

        access = source_command.access
        rights_basis = (
            access.permission_notes.strip()
            or (access.license_name or "").strip()
            or f"Registered access method: {access.access_method.strip()}"
        )
        rights_evidence = [
            value
            for value in (
                (access.rights_uri or "").strip(),
                (
                    f"access-metadata-sha256:"
                    f"{canonical_payload_hash(access.model_dump(mode='json'))}"
                ),
            )
            if value
        ]
        command = SourcePermissionRequestCommand(
            source_version_id=version.id,
            requested_extraction_permission=(
                source_command.requested_extraction_permission
            ),
            requested_storage_permission=source_command.requested_storage_permission,
            requested_scope_locators=source_command.requested_scope_locators,
            rights_basis=rights_basis,
            rights_evidence=rights_evidence,
            reason=(
                "Initial permission request captured from immutable Source "
                "registration metadata."
            ),
        )
        self._validate_permission_request(version, command)
        return self._create_permission_request(
            session,
            version,
            command,
            actor,
            roles,
            sequence_number=1,
            supersedes=None,
        )

    def _create_permission_request(
        self,
        session: Session,
        version: SourceVersion,
        command: SourcePermissionRequestCommand,
        actor: AuthenticatedActor,
        roles: frozenset[RoleName],
        *,
        sequence_number: int,
        supersedes: SourcePermissionRequest | None,
    ) -> SourcePermissionRequest:
        assert actor.user_id is not None
        payload = permission_request_checksum_payload(
            source_version_id=version.id,
            sequence_number=sequence_number,
            requested_extraction_permission=command.requested_extraction_permission,
            requested_storage_permission=command.requested_storage_permission,
            requested_scope_locators=command.requested_scope_locators,
            rights_basis=command.rights_basis,
            rights_evidence=command.rights_evidence,
            reason=command.reason,
            submitted_by_user_id=actor.user_id,
            actor_role_snapshot=sorted(role.value for role in roles),
            supersedes_request_id=(supersedes.id if supersedes is not None else None),
        )
        checksum = canonical_payload_hash(payload)
        row = SourcePermissionRequest(
            id=uuid5(
                NAMESPACE_URL,
                (
                    "magicforge:source-permission-request:"
                    f"{version.id}:{sequence_number}:{checksum}"
                ),
            ),
            source_version_id=version.id,
            sequence_number=sequence_number,
            requested_extraction_permission=(
                command.requested_extraction_permission
            ),
            requested_storage_permission=command.requested_storage_permission,
            requested_scope_locators=command.requested_scope_locators,
            rights_basis=command.rights_basis,
            rights_evidence=command.rights_evidence,
            reason=command.reason,
            submitted_by_user_id=actor.user_id,
            actor_role_snapshot=sorted(role.value for role in roles),
            domain_schema_version=SOURCE_PERMISSION_REQUEST_SCHEMA_VERSION,
            request_checksum=checksum,
            supersedes_request_id=(supersedes.id if supersedes is not None else None),
            created_at=self._now(),
        )
        session.add(row)
        session.flush()
        return row

    def _validate_permission_request(
        self,
        version: SourceVersion,
        command: SourcePermissionRequestCommand,
    ) -> None:
        if command.source_version_id != version.id:
            raise GovernanceConflictError(
                "permission_request_version_mismatch",
                "The permission request does not match the immutable Source version.",
            )
        if version.content_access in {
            ContentAccess.SEARCH_SNIPPET,
            ContentAccess.ABSTRACT,
        } and (
            command.requested_extraction_permission
            in {
                ExtractionPermission.SELECTED_SECTIONS,
                ExtractionPermission.FULL_TEXT,
            }
            or command.requested_storage_permission != StoragePermission.NONE
        ):
            raise GovernanceConflictError(
                "claim_level_content_required",
                "Claim-level extraction or storage cannot be requested for "
                "snippet or abstract content.",
            )
        if (
            command.requested_storage_permission
            == StoragePermission.DERIVED_WITH_SHORT_EXCERPT
            and not bool(version.access_metadata.get("redistribution_allowed", False))
        ):
            raise GovernanceConflictError(
                "redistribution_not_allowed",
                "Short-excerpt storage cannot be requested without redistribution rights.",
            )

    def _build_citation_evidence(
        self,
        version: SourceVersion,
        verification: CitationVerificationInput,
        actor: AuthenticatedActor,
        roles: frozenset[RoleName],
    ) -> CitationVerificationEvidence:
        assert actor.user_id is not None
        if not self._verification_matches(version, verification):
            raise GovernanceInputError(
                "citation_verification_mismatch",
                "Citation verification evidence does not match the immutable Source version.",
            )
        method = {
            CitationVerificationMethod.DOI_RESOLVER: VerificationMethod.METADATA_RESOLVER,
            CitationVerificationMethod.CANONICAL_LOCATOR: VerificationMethod.CANONICAL_LOCATOR,
            CitationVerificationMethod.MANUAL_METADATA: VerificationMethod.MANUAL_REVIEW,
            CitationVerificationMethod.CONTENT_CHECKSUM: VerificationMethod.FULL_TEXT_LOCATOR,
        }[verification.method]
        now = self._now()
        result = {
            "outcome": verification.resolver_result.value,
            "resolver_name": verification.resolver_name,
            "scope": verification.scope.value,
            "actor_role_snapshot": sorted(role.value for role in roles),
        }
        payload = {
            "source_version_id": str(version.id),
            "citation_id": str(version.citation_id),
            "scope": verification.scope.value,
            "method": method.value,
            "resolver_result": result,
            "verified_identifier": verification.verified_identifier.strip(),
            "checked_locator": verification.checked_locator.strip(),
            "reviewer_user_id": str(actor.user_id),
            "verified_at": now.isoformat(),
            "notes": verification.notes.strip(),
        }
        checksum = canonical_payload_hash(payload)
        return CitationVerificationEvidence(
            id=uuid5(
                NAMESPACE_URL,
                f"magicforge:citation-verification:{version.id}:{checksum}",
            ),
            source_version_id=version.id,
            citation_id=version.citation_id,
            verification_scope=VerificationScope(verification.scope.value),
            verification_method=method,
            resolver_result=result,
            verified_identifier=verification.verified_identifier.strip(),
            checked_locator=verification.checked_locator.strip(),
            reviewer_user_id=actor.user_id,
            evidence_checksum=checksum,
            domain_schema_version="citation-verification-evidence-0.1",
            notes=verification.notes.strip(),
            verified_at=now,
        )

    def _verification_matches(
        self,
        version: SourceVersion,
        verification: CitationVerificationInput,
    ) -> bool:
        citation = version.citation_metadata
        identifier = verification.verified_identifier.strip()
        if verification.method == CitationVerificationMethod.CONTENT_CHECKSUM:
            identifier_matches = identifier.casefold() == version.content_hash
        elif verification.method == CitationVerificationMethod.DOI_RESOLVER:
            identifier_matches = bool(
                citation.get("doi")
                and _canonical_doi(identifier) == _canonical_doi(str(citation["doi"]))
            )
        else:
            expected = {
                _canonical_url(str(citation.get("url") or "")),
                *(
                    {_canonical_doi(str(citation["doi"]))}
                    if citation.get("doi")
                    else set()
                ),
            }
            normalized = (
                _canonical_url(identifier)
                if "://" in identifier
                else _canonical_doi(identifier)
            )
            identifier_matches = normalized in expected
        return identifier_matches and _checked_locator_matches(
            version, verification.checked_locator
        )

    def _citation_status(self, session: Session, version: SourceVersion) -> CitationStatus:
        return self._citation_status_with_evidence(session, version)[0]

    def _citation_status_with_evidence(
        self, session: Session, version: SourceVersion
    ) -> tuple[CitationStatus, CitationVerificationEvidence | None]:
        citation = version.citation_metadata
        missing = [field for field in ("title", "url") if not str(citation.get(field) or "").strip()]
        if citation.get("source_category") == SourceCategory.ACADEMIC.value:
            if not citation.get("authors"):
                missing.append("authors")
            if citation.get("year") is None:
                missing.append("year")
        if missing:
            return CitationStatus.INCOMPLETE, None
        evidence = list(
            session.scalars(
                select(CitationVerificationEvidence)
                .where(CitationVerificationEvidence.source_version_id == version.id)
                .order_by(CitationVerificationEvidence.verified_at.asc())
            )
        )
        integrity_valid = [
            item
            for item in evidence
            if self._stored_evidence_integrity_valid(item, version)
        ]
        valid = [
            item for item in integrity_valid if self._stored_evidence_valid(item, version)
        ]
        metadata = [
            item
            for item in valid
            if item.verification_scope == VerificationScope.METADATA
        ]
        full_text = [
            item
            for item in valid
            if item.verification_scope == VerificationScope.FULL_TEXT
            and item.verified_identifier
            and item.verified_identifier.casefold() == version.content_hash
        ]
        if metadata and full_text:
            return CitationStatus.FULL_TEXT_VERIFIED, full_text[-1]
        if metadata:
            return CitationStatus.METADATA_VERIFIED, metadata[-1]
        if evidence and not integrity_valid:
            return CitationStatus.FAILED, None
        return CitationStatus.UNVERIFIED, None

    def _stored_evidence_valid(
        self,
        item: CitationVerificationEvidence,
        version: SourceVersion,
    ) -> bool:
        if not self._stored_evidence_integrity_valid(item, version):
            return False
        role_snapshot = item.resolver_result.get("actor_role_snapshot", [])
        return bool(
            isinstance(role_snapshot, list)
            and RoleName.REVIEWER.value in role_snapshot
            and item.reviewer_user_id != version.submitted_by_user_id
        )

    def _stored_evidence_integrity_valid(
        self,
        item: CitationVerificationEvidence,
        version: SourceVersion,
    ) -> bool:
        payload = {
            "source_version_id": str(item.source_version_id),
            "citation_id": str(item.citation_id),
            "scope": item.verification_scope.value,
            "method": item.verification_method.value,
            "resolver_result": item.resolver_result,
            "verified_identifier": item.verified_identifier,
            "checked_locator": item.checked_locator,
            "reviewer_user_id": str(item.reviewer_user_id),
            "verified_at": _aware_utc(item.verified_at).isoformat(),
            "notes": item.notes,
        }
        if canonical_payload_hash(payload) != item.evidence_checksum:
            return False
        if item.source_version_id != version.id or item.citation_id != version.citation_id:
            return False
        if item.resolver_result.get("outcome") != ResolverResult.MATCHED.value:
            return False
        if not _checked_locator_matches(version, item.checked_locator):
            return False
        if item.verification_scope == VerificationScope.FULL_TEXT:
            return bool(
                item.verification_method == VerificationMethod.FULL_TEXT_LOCATOR
                and item.verified_identifier.casefold() == version.content_hash
            )
        citation = version.citation_metadata
        allowed = {_canonical_url(str(citation.get("url") or ""))}
        if citation.get("doi"):
            allowed.add(_canonical_doi(str(citation["doi"])))
        identifier = item.verified_identifier
        normalized = (
            _canonical_url(identifier)
            if "://" in identifier
            else _canonical_doi(identifier)
        )
        return normalized in allowed

    def _validate_source_decision(
        self,
        command: SourceReviewCommand,
        version: SourceVersion,
        permission_request: SourcePermissionRequest,
        citation_status: CitationStatus,
        actor: AuthenticatedActor,
    ) -> None:
        if command.decision == ReviewDecision.REJECT:
            return
        if citation_status not in {
            CitationStatus.METADATA_VERIFIED,
            CitationStatus.FULL_TEXT_VERIFIED,
        }:
            raise GovernanceConflictError(
                "citation_not_verified",
                "Source approval requires evidence-derived citation verification.",
            )
        if version.content_access in {ContentAccess.SEARCH_SNIPPET, ContentAccess.ABSTRACT}:
            raise GovernanceConflictError(
                "claim_level_content_required",
                "Source approval requires acquired claim-level content.",
            )
        if not command.claim_eligibility.allows_projection:
            raise GovernanceInputError(
                "claim_eligibility_required", "An approved Source must be claim-eligible."
            )
        if not command.extraction_permission.allows_claim_extraction:
            raise GovernanceInputError(
                "extraction_permission_required",
                "An approved Source must permit claim extraction.",
            )
        if (
            command.extraction_permission == ExtractionPermission.SELECTED_SECTIONS
            and not command.extraction_scope_locators
        ):
            raise GovernanceInputError(
                "extraction_scope_required",
                "Selected-section approval requires explicit locators.",
            )
        requested_extraction = permission_request.requested_extraction_permission
        requested_storage = permission_request.requested_storage_permission
        if _extraction_rank(command.extraction_permission) > _extraction_rank(requested_extraction):
            raise GovernanceConflictError(
                "extraction_permission_exceeded",
                "The review decision exceeds the latest permission request.",
            )
        if requested_extraction == ExtractionPermission.SELECTED_SECTIONS:
            requested_scope = {
                str(value).strip().casefold()
                for value in permission_request.requested_scope_locators
                if str(value).strip()
            }
            approved_scope = {
                value.strip().casefold()
                for value in command.extraction_scope_locators
                if value.strip()
            }
            if not approved_scope.issubset(requested_scope):
                raise GovernanceConflictError(
                    "extraction_scope_exceeded",
                    "The approved extraction scope exceeds the registered locators.",
                )
        if _storage_rank(command.storage_permission) > _storage_rank(requested_storage):
            raise GovernanceConflictError(
                "storage_permission_exceeded",
                "The review decision exceeds the latest permission request.",
            )
        if (
            command.storage_permission
            == StoragePermission.DERIVED_WITH_SHORT_EXCERPT
            and not bool(version.access_metadata.get("redistribution_allowed", False))
        ):
            raise GovernanceConflictError(
                "redistribution_not_allowed",
                "Short-excerpt storage requires redistribution rights.",
            )
        if _sensitivity_rank(command.sensitive_information_level) < _sensitivity_rank(version.sensitivity):
            raise GovernanceConflictError(
                "sensitivity_downgrade_forbidden",
                "A review decision cannot lower the Source sensitivity classification.",
            )
        self._ensure_sensitivity_visible(actor, command.sensitive_information_level)
        try:
            domain_record = SourceApprovalRecord(
                source_candidate_id=str(version.source_id),
                citation_id=str(version.citation_id),
                citation_status=citation_status,
                source_version_id=str(version.id),
                source_permission_request_id=str(permission_request.id),
                content_hash=version.content_hash,
                content_access=version.content_access,
                source_type=version.source_type,
                extraction_scope=ExtractionScope(
                    locators=command.extraction_scope_locators
                ),
                status=ReviewStatus.APPROVED,
                reviewer=actor.username,
                review_date=self._now(),
                approval_reason=command.reason.strip(),
                claim_eligibility=command.claim_eligibility,
                extraction_permission=command.extraction_permission,
                storage_permission=command.storage_permission,
                sensitive_information_level=command.sensitive_information_level,
                contradicting_evidence_checked=(
                    command.contradicting_evidence_checked
                ),
                submitted_at=version.created_at,
            )
        except ValidationError as exc:
            raise GovernanceInputError(
                "source_approval_invalid",
                "The review does not form a valid Source approval record.",
            ) from exc
        if UUID(domain_record.source_version_id) != version.id:
            raise GovernanceConflictError(
                "source_version_identity_mismatch",
                "The immutable Source version identity is inconsistent.",
            )
        if UUID(domain_record.source_permission_request_id) != permission_request.id:
            raise GovernanceConflictError(
                "source_permission_request_identity_mismatch",
                "The Source approval is not bound to its permission request.",
            )

    def _validate_claim_candidate(
        self,
        command: ClaimCandidateCommand,
        version: SourceVersion,
        source_decision: SourceReviewDecision,
    ) -> None:
        if not evidence_excerpt_is_locatable(command.evidence_excerpt, version.content_text):
            raise GovernanceInputError(
                "evidence_excerpt_not_locatable",
                "The Claim excerpt is not locatable in the immutable Source version.",
            )
        origin, _ = classification_for_evidence_class(command.proposed_evidence_class)
        if (
            version.knowledge_origin == KnowledgeOrigin.EXPERT_PRACTICE
            and origin == KnowledgeOrigin.SCIENTIFIC_EVIDENCE
        ):
            raise GovernanceInputError(
                "practitioner_scientific_leakage",
                "Practitioner material cannot enter the scientific evidence channel.",
            )
        if (
            version.knowledge_origin == KnowledgeOrigin.PERSONAL_INTERPRETATION
            and origin != KnowledgeOrigin.PERSONAL_INTERPRETATION
        ):
            raise GovernanceInputError(
                "interpretation_origin_mismatch",
                "Personal interpretation cannot be promoted into another evidence channel.",
            )
        if source_decision.extraction_permission == ExtractionPermission.SELECTED_SECTIONS:
            allowed = {
                str(value).strip().casefold()
                for value in source_decision.extraction_scope.get("locators", [])
                if str(value).strip()
            }
            candidates = {
                command.locator.source_locator.strip().casefold(),
                (command.locator.section or "").strip().casefold(),
                (
                    f"page {command.locator.page_number}"
                    if command.locator.page_number
                    else ""
                ),
            }
            if not (allowed & candidates):
                raise GovernanceConflictError(
                    "claim_outside_extraction_scope",
                    "The Claim locator is outside the approved extraction scope.",
                )

    def _validate_claim_approval(
        self,
        candidate: ClaimCandidate,
        command: ClaimReviewCommand,
        actor: AuthenticatedActor,
        source_decision: SourceReviewDecision,
    ) -> None:
        if candidate.claim_role == ClaimRole.CONTEXT_ONLY or not candidate.evidence_card_eligible:
            raise GovernanceConflictError(
                "context_only_not_evidence",
                "A context_only Claim cannot create an Evidence Card.",
            )
        if command.confidence is None:
            raise GovernanceInputError(
                "confidence_required", "Claim approval requires a confidence assessment."
            )
        if command.confidence.assessed_by.strip() != actor.username:
            raise GovernanceInputError(
                "confidence_reviewer_mismatch",
                "The confidence assessor must be the authenticated reviewer.",
            )
        if command.confidence.label == ConfidenceLabel.INSUFFICIENT:
            raise GovernanceInputError(
                "confidence_insufficient",
                "Insufficient confidence cannot approve an Evidence Card.",
            )
        if not command.claim_eligibility.allows_projection:
            raise GovernanceInputError(
                "claim_eligibility_required", "An approved Claim must be eligible."
            )
        if not command.storage_permission.allows_projection:
            raise GovernanceInputError(
                "storage_eligibility_required",
                "An approved Evidence Card requires explicit storage eligibility.",
            )
        if _storage_rank(command.storage_permission) > _storage_rank(source_decision.storage_permission):
            raise GovernanceConflictError(
                "storage_permission_exceeded",
                "Claim storage permission exceeds the approved Source permission.",
            )
        if not command.contradicting_evidence_checked.completed:
            raise GovernanceInputError(
                "contradiction_check_required",
                "Claim approval requires a completed contradiction check.",
            )
        if command.contradiction_status == ContradictionStatus.NOT_CHECKED:
            raise GovernanceInputError(
                "contradiction_status_required",
                "Claim approval requires an explicit contradiction status.",
            )
        if (
            command.contradiction_status
            in {ContradictionStatus.RESOLVED, ContradictionStatus.UNRESOLVED}
            and not command.contradicting_evidence_ids
        ):
            raise GovernanceInputError(
                "contradicting_evidence_required",
                "Resolved or unresolved contradictions require linked Evidence Cards.",
            )
        if not command.limitations:
            raise GovernanceInputError(
                "limitations_required", "Claim approval requires explicit limitations."
            )
        if _sensitivity_rank(command.sensitive_information_level) < _sensitivity_rank(
            source_decision.sensitive_information_level
        ):
            raise GovernanceConflictError(
                "sensitivity_downgrade_forbidden",
                "A Claim cannot lower its approved Source sensitivity classification.",
            )
        required = _sensitivity_for_exposure(command.secret_exposure_level)
        if _sensitivity_rank(command.sensitive_information_level) < _sensitivity_rank(required):
            raise GovernanceConflictError(
                "secret_exposure_misclassified",
                "The sensitivity classification is too low for the secret exposure level.",
            )
        self._ensure_sensitivity_visible(actor, command.sensitive_information_level)

    def _build_evidence_card(
        self,
        session: Session,
        candidate: ClaimCandidate,
        version: SourceVersion,
        source_decision: SourceReviewDecision,
        command: ClaimReviewCommand,
        actor: AuthenticatedActor,
        decision_id: UUID,
    ) -> EvidenceCard:
        assert command.confidence is not None
        proposal = candidate.proposed_evidence_card
        citation_status = self._citation_status(session, version)
        if citation_status not in {
            CitationStatus.METADATA_VERIFIED,
            CitationStatus.FULL_TEXT_VERIFIED,
        }:
            raise GovernanceConflictError(
                "citation_verification_lost",
                "The Claim's citation verification is no longer valid.",
            )
        source_category = SourceCategory(
            version.citation_metadata.get("source_category", SourceCategory.WEB.value)
        )
        confidence = ConfidenceAssessment.model_validate(
            {
                **command.confidence.model_dump(mode="python"),
                "assessed_by": actor.username,
                "assessed_at": self._now(),
            }
        )
        origin, evidence_level = classification_for_evidence_class(
            EvidenceClass(candidate.proposed_evidence_card["proposed_evidence_class"])
        )
        research_paper_id = (
            str(
                uuid5(
                    NAMESPACE_URL,
                    f"magicforge:research-paper:{version.source_id}",
                )
            )
            if source_category == SourceCategory.ACADEMIC
            else None
        )
        card = EvidenceCard(
            version=1,
            canonical_claim_id=str(candidate.canonical_claim_id),
            claim=candidate.claim_text,
            claim_role=candidate.claim_role,
            claim_polarity=candidate.claim_polarity,
            mechanism_ids=[str(value) for value in proposal.get("mechanism_ids", [])],
            mechanism_status=proposal["mechanism_status"],
            principle_ids=[str(value) for value in proposal.get("principle_ids", [])],
            applicable_domain=proposal["applicable_domain"],
            ontology_paths=proposal["ontology_paths"],
            topic_tags=proposal.get("topic_tags", []),
            magic_application=proposal.get("magic_application"),
            application_origin=proposal["application_origin"],
            knowledge_origin=origin,
            evidence_class=proposal["proposed_evidence_class"],
            evidence_level=evidence_level,
            source=EvidenceSource(
                citation_id=str(version.citation_id),
                source_id=str(version.source_id),
                source_candidate_id=str(version.source_id),
                source_version_id=str(version.id),
                document_id=str(version.id),
                source_type=version.source_type,
                research_paper_id=research_paper_id,
                citation_status=citation_status.value,
                peer_review_status=str(
                    version.citation_metadata.get("peer_review_status", "unknown")
                ),
                source_year=version.citation_metadata.get("year"),
            ),
            locator=proposal["locator"],
            evidence_excerpt=proposal["evidence_excerpt"],
            limitations=command.limitations,
            population_context=proposal.get("population_context"),
            performance_context=proposal.get("performance_context"),
            confidence=confidence,
            extraction_confidence=candidate.extraction_confidence,
            contradiction_status=command.contradiction_status,
            contradicting_evidence_ids=[
                str(value) for value in command.contradicting_evidence_ids
            ],
            review=EvidenceReview(
                review_item_id=str(decision_id),
                claim_eligibility=command.claim_eligibility,
                extraction_permission=source_decision.extraction_permission,
                storage_permission=command.storage_permission,
                approved=True,
                review_status=ReviewStatus.APPROVED,
                reviewer=actor.username,
                review_date=self._now(),
                approval_reason=command.reason.strip(),
                contradicting_evidence_checked=command.contradicting_evidence_checked,
                sensitive_information_level=command.sensitive_information_level,
            ),
            secret_exposure_level=command.secret_exposure_level,
            created_at=self._now(),
            created_by=str(
                candidate.extraction_provenance.get("producer", "pipeline")
            ),
        )
        review_item_id = str(
            uuid5(
                NAMESPACE_URL,
                f"magicforge:claim-review:{card.id}:{card.version}",
            )
        )
        approved_card = EvidenceCard.model_validate(
            {
                **card.model_dump(mode="python"),
                "review": {
                    **card.review.model_dump(mode="python"),
                    "review_item_id": review_item_id,
                },
            }
        )
        compatibility_item = ClaimReviewItem(
            card=approved_card,
            status=ReviewStatus.APPROVED,
            reviewed_at=self._now(),
        )
        if approved_card.review.review_item_id != compatibility_item.id:
            raise GovernanceConflictError(
                "claim_review_identity_mismatch",
                "The Claim review identity is inconsistent.",
            )
        return approved_card

    def _source_summary(
        self,
        session: Session,
        source: Source,
        *,
        allowed_sensitive_levels: set[SensitiveInformationLevel] | None = None,
    ) -> SourceSummary:
        versions = list(
            session.scalars(
                select(SourceVersion)
                .where(SourceVersion.source_id == source.id)
                .order_by(SourceVersion.version_number.asc())
            )
        )
        classified_versions = [
            (version, self._effective_source_sensitivity(session, version))
            for version in versions
        ]
        if allowed_sensitive_levels is not None:
            classified_versions = [
                (version, sensitivity)
                for version, sensitivity in classified_versions
                if sensitivity in allowed_sensitive_levels
            ]
        summaries: list[SourceVersionSummary] = []
        for version, sensitivity in classified_versions:
            latest_permission_request = self._latest_source_permission_request(
                session, version.id
            )
            if latest_permission_request is not None:
                self._require_latest_permission_request_integrity(
                    session, latest_permission_request
                )
            summaries.append(SourceVersionSummary(
                id=version.id,
                version=version.version_number,
                content_hash=version.content_hash,
                content_access=version.content_access,
                source_type=version.source_type,
                knowledge_origin=version.knowledge_origin,
                sensitivity=sensitivity,
                citation_status=self._citation_status(session, version).value,
                status=self._source_state(session, version.id),
                latest_permission_request_id=(
                    latest_permission_request.id
                    if latest_permission_request is not None
                    else None
                ),
                created_at=version.created_at,
            ))
        latest = classified_versions[-1][0] if classified_versions else None
        title = str(latest.citation_metadata.get("title") or "") if latest else ""
        source_category = SourceCategory(
            latest.citation_metadata.get("source_category", SourceCategory.WEB.value)
        ) if latest else SourceCategory.WEB
        return SourceSummary(
            id=source.id,
            canonical_key=source.canonical_key,
            title=title,
            source_category=source_category,
            versions=summaries,
        )

    def _source_summary_for_actor(
        self,
        session: Session,
        source: Source,
        actor: AuthenticatedActor,
    ) -> SourceSummary:
        authorization = retrieval_authorization_for_actor(actor)
        return self._source_summary(
            session,
            source,
            allowed_sensitive_levels=set(authorization.allowed_sensitive_levels),
        )

    def _source_review_detail(
        self,
        session: Session,
        source: Source,
        *,
        allowed_sensitive_levels: set[SensitiveInformationLevel],
    ) -> SourceReviewDetail:
        versions = list(
            session.scalars(
                select(SourceVersion)
                .where(SourceVersion.source_id == source.id)
                .order_by(SourceVersion.version_number.asc())
            )
        )
        classified_versions = [
            (version, sensitivity)
            for version in versions
            if (
                sensitivity := self._effective_source_sensitivity(session, version)
            )
            in allowed_sensitive_levels
        ]
        details: list[SourceVersionReviewView] = []
        for version, sensitivity in classified_versions:
            citation_payload = {
                key: value
                for key, value in version.citation_metadata.items()
                if key != "source_category"
            }
            access_payload = {
                key: version.access_metadata.get(key)
                for key in (
                    "access_method",
                    "license_name",
                    "rights_uri",
                    "permission_notes",
                    "redistribution_allowed",
                )
            }
            evidence_rows = list(
                session.scalars(
                    select(CitationVerificationEvidence)
                    .where(
                        CitationVerificationEvidence.source_version_id == version.id
                    )
                    .order_by(
                        CitationVerificationEvidence.verified_at.asc(),
                        CitationVerificationEvidence.id.asc(),
                    )
                )
            )
            evidence_views: list[CitationVerificationEvidenceView] = []
            for evidence in evidence_rows:
                reviewer = UserRepository(session).get(evidence.reviewer_user_id)
                result = evidence.resolver_result
                try:
                    resolver_result = ResolverResult(str(result.get("outcome")))
                except ValueError:
                    resolver_result = ResolverResult.ERROR
                evidence_views.append(
                    CitationVerificationEvidenceView(
                        id=evidence.id,
                        scope=CitationVerificationScope(
                            evidence.verification_scope.value
                        ),
                        method=evidence.verification_method.value,
                        resolver_result=resolver_result,
                        resolver_name=str(result.get("resolver_name") or ""),
                        verified_identifier=evidence.verified_identifier,
                        checked_locator=evidence.checked_locator,
                        review_actor=(reviewer.username if reviewer else "unknown"),
                        review_timestamp=evidence.verified_at,
                        evidence_checksum=evidence.evidence_checksum,
                        actor_role_snapshot=[
                            str(value)
                            for value in result.get("actor_role_snapshot", [])
                        ],
                        trusted_for_status=self._stored_evidence_valid(
                            evidence, version
                        ),
                        notes=evidence.notes,
                    )
                )
            permission_rows = list(
                session.scalars(
                    select(SourcePermissionRequest)
                    .where(SourcePermissionRequest.source_version_id == version.id)
                    .order_by(
                        SourcePermissionRequest.sequence_number.asc(),
                        SourcePermissionRequest.id.asc(),
                    )
                )
            )
            if permission_rows:
                self._require_latest_permission_request_integrity(
                    session,
                    permission_rows[-1],
                )
            permission_views = [
                self._permission_request_view(session, row)
                for row in permission_rows
            ]
            latest_permission_view = (
                permission_views[-1] if permission_views else None
            )
            details.append(
                SourceVersionReviewView(
                    id=version.id,
                    version=version.version_number,
                    content_hash=version.content_hash,
                    content_access=version.content_access,
                    source_type=version.source_type,
                    knowledge_origin=version.knowledge_origin,
                    sensitivity=sensitivity,
                    citation_status=self._citation_status(session, version).value,
                    status=self._source_state(session, version.id),
                    latest_permission_request_id=(
                        latest_permission_view.id
                        if latest_permission_view is not None
                        else None
                    ),
                    created_at=version.created_at,
                    citation=citation_payload,
                    access=access_payload,
                    requested_extraction_permission=(
                        _normalized_requested_extraction_permission(
                            version.access_metadata.get(
                                "requested_extraction_permission"
                            )
                        )
                    ),
                    requested_storage_permission=(
                        _normalized_requested_storage_permission(
                            version.access_metadata.get(
                                "requested_storage_permission"
                            )
                        )
                    ),
                    requested_scope_locators=[
                        str(value)
                        for value in _normalized_requested_scope(
                            version.access_metadata.get(
                                "requested_scope_locators"
                            )
                        )
                    ],
                    content=version.content_text,
                    citation_verification=evidence_views,
                    latest_permission_request=latest_permission_view,
                    permission_requests=permission_views,
                )
            )
        latest = classified_versions[-1][0] if classified_versions else None
        return SourceReviewDetail(
            id=source.id,
            canonical_key=source.canonical_key,
            title=(
                str(latest.citation_metadata.get("title") or "") if latest else ""
            ),
            source_category=(
                SourceCategory(
                    latest.citation_metadata.get(
                        "source_category", SourceCategory.WEB.value
                    )
                )
                if latest
                else SourceCategory.WEB
            ),
            versions=details,
        )

    def _claim_summary(
        self,
        session: Session,
        candidate: ClaimCandidate,
        *,
        sensitivity: SensitiveInformationLevel | None = None,
    ) -> ClaimCandidateSummary:
        sensitivity = sensitivity or self._effective_claim_sensitivity(
            session, candidate
        )
        return ClaimCandidateSummary(
            id=candidate.id,
            source_version_id=candidate.source_version_id,
            claim=candidate.claim_text,
            claim_role=candidate.claim_role,
            proposed_evidence_class=candidate.proposed_evidence_card[
                "proposed_evidence_class"
            ],
            status=WorkflowStatus(candidate.status.value),
            candidate_checksum=candidate.candidate_checksum,
            sensitivity=sensitivity,
            submitted_at=candidate.created_at,
        )

    def _claim_review_detail(
        self,
        session: Session,
        candidate: ClaimCandidate,
    ) -> ClaimCandidateReviewView:
        summary = self._claim_summary(session, candidate)
        proposal = candidate.proposed_evidence_card
        source_version = session.get(SourceVersion, candidate.source_version_id)
        if source_version is None:
            raise GovernanceConflictError(
                "source_version_missing",
                "The Claim's immutable Source version is unavailable.",
            )
        source = session.get(Source, source_version.source_id)
        if source is None:
            raise GovernanceConflictError(
                "source_missing", "The Claim's Source is unavailable."
            )
        citation_payload = {
            key: value
            for key, value in source_version.citation_metadata.items()
            if key != "source_category"
        }
        access_payload = {
            key: source_version.access_metadata.get(key)
            for key in (
                "access_method",
                "license_name",
                "rights_uri",
                "permission_notes",
                "redistribution_allowed",
            )
        }
        source_context = ClaimSourceContextView(
            source_id=source.id,
            source_version_id=source_version.id,
            version=source_version.version_number,
            title=str(source_version.citation_metadata.get("title") or ""),
            source_category=SourceCategory(
                source_version.citation_metadata.get(
                    "source_category", SourceCategory.WEB.value
                )
            ),
            citation=CitationMetadataInput.model_validate(citation_payload),
            access=SourceAccessMetadata.model_validate(access_payload),
            content=source_version.content_text,
            content_access=source_version.content_access,
            source_type=source_version.source_type,
            knowledge_origin=source_version.knowledge_origin,
            sensitivity=self._effective_source_sensitivity(
                session, source_version
            ),
            citation_status=self._citation_status(session, source_version).value,
        )
        return ClaimCandidateReviewView(
            **summary.model_dump(mode="python"),
            claim_polarity=candidate.claim_polarity,
            applicable_domain=proposal["applicable_domain"],
            ontology_paths=proposal["ontology_paths"],
            topic_tags=proposal.get("topic_tags", []),
            mechanism_ids=proposal.get("mechanism_ids", []),
            mechanism_status=proposal["mechanism_status"],
            principle_ids=proposal.get("principle_ids", []),
            magic_application=proposal.get("magic_application"),
            application_origin=proposal["application_origin"],
            locator=candidate.locator,
            evidence_excerpt=proposal["evidence_excerpt"],
            proposed_limitations=proposal["proposed_limitations"],
            population_context=proposal.get("population_context"),
            performance_context=proposal.get("performance_context"),
            extraction_confidence=candidate.extraction_confidence,
            extraction_provenance=candidate.extraction_provenance,
            candidate_schema_version=candidate.candidate_schema_version,
            source_context=source_context,
        )

    def _claim_visible(
        self,
        session: Session,
        actor: AuthenticatedActor,
        candidate: ClaimCandidate,
    ) -> bool:
        authorization = retrieval_authorization_for_actor(actor)
        source_version = session.get(SourceVersion, candidate.source_version_id)
        source_decision = (
            self._latest_source_decision(session, candidate.source_version_id)
            if source_version is not None
            else None
        )
        if (
            source_version is None
            or source_decision is None
            or source_decision.resulting_status
            != GovernanceWorkflowStatus.APPROVED
            or not self._source_approval_is_current(
                session,
                source_version,
                source_decision,
            )
        ):
            return False
        claim_sensitivity = self._effective_claim_sensitivity(session, candidate)
        source_sensitivity = self._effective_source_sensitivity(
            session, source_version
        )
        return (
            claim_sensitivity in authorization.allowed_sensitive_levels
            and source_sensitivity in authorization.allowed_sensitive_levels
        )

    def _current_evidence_card(
        self,
        session: Session,
        row: EvidenceCardVersion,
    ) -> tuple[EvidenceCard, SensitiveInformationLevel] | None:
        """Return one exact, currently approved Evidence row or fail closed.

        Product Evidence reads must not trust the JSON approval snapshot alone.
        Source policy can be corrected after an Evidence version was created and
        another classified Claim can raise the effective Source classification.
        Any integrity, identity, or current-policy mismatch therefore makes the
        row undiscoverable rather than exposing stale material.
        """

        if canonical_payload_hash(row.payload) != row.payload_checksum:
            return None
        try:
            card = EvidenceCard.model_validate(row.payload)
        except ValidationError:
            return None
        try:
            identity_matches = (
                UUID(card.id) == row.domain_object_id
                and card.version == row.version_number
                and card.schema_version == row.domain_schema_version
                and UUID(card.source.source_id) == row.source_id
                and UUID(card.source.source_version_id) == row.source_version_id
                and UUID(card.source.citation_id) == row.citation_id
                and UUID(card.canonical_claim_id) == row.canonical_claim_id
            )
        except (TypeError, ValueError):
            return None
        if not identity_matches:
            return None

        candidate = session.get(ClaimCandidate, row.claim_candidate_id)
        claim_decision = self._latest_claim_decision(
            session, row.claim_candidate_id
        )
        source_decision = self._latest_source_decision(
            session, row.source_version_id
        )
        latest_version = session.scalar(
            select(EvidenceCardVersion)
            .where(EvidenceCardVersion.evidence_card_id == row.evidence_card_id)
            .order_by(
                EvidenceCardVersion.version_number.desc(),
                EvidenceCardVersion.created_at.desc(),
                EvidenceCardVersion.id.desc(),
            )
            .limit(1)
        )
        if (
            candidate is None
            or candidate.status != GovernanceWorkflowStatus.APPROVED
            or not candidate.evidence_card_eligible
            or claim_decision is None
            or claim_decision.id != row.claim_review_decision_id
            or claim_decision.resulting_status
            != GovernanceWorkflowStatus.APPROVED
            or source_decision is None
            or source_decision.resulting_status
            != GovernanceWorkflowStatus.APPROVED
            or latest_version is None
            or latest_version.id != row.id
            or not card.review.approved
            or card.review.review_status
            not in {ReviewStatus.APPROVED, ReviewStatus.INGESTED}
            or not card.review.claim_eligibility.allows_projection
            or not card.review.storage_permission.allows_projection
            or not card.review.contradicting_evidence_checked.completed
        ):
            return None

        source_version = session.get(SourceVersion, row.source_version_id)
        if (
            source_version is None
            or not self._source_approval_is_current(
                session,
                source_version,
                source_decision,
            )
        ):
            return None

        expected_eligibility = min(
            (
                source_decision.claim_eligibility,
                claim_decision.claim_eligibility,
            ),
            key=_claim_eligibility_rank,
        )
        expected_storage = min(
            (
                source_decision.storage_permission,
                claim_decision.storage_permission,
            ),
            key=_storage_rank,
        )
        expected_sensitivity = max(
            (
                source_decision.sensitive_information_level,
                claim_decision.sensitive_information_level,
            ),
            key=_sensitivity_rank,
        )
        if (
            not source_decision.claim_eligibility.allows_projection
            or not claim_decision.claim_eligibility.allows_projection
            or not source_decision.extraction_permission.allows_claim_extraction
            or not source_decision.storage_permission.allows_projection
            or not claim_decision.storage_permission.allows_projection
            or card.review.claim_eligibility != expected_eligibility
            or card.review.claim_eligibility != claim_decision.claim_eligibility
            or card.review.extraction_permission
            != source_decision.extraction_permission
            or card.review.storage_permission != expected_storage
            or card.review.sensitive_information_level != expected_sensitivity
            or card.knowledge_origin != claim_decision.knowledge_origin
            or card.evidence_class != claim_decision.evidence_class
            or card.evidence_level != claim_decision.evidence_level
            or card.contradiction_status != claim_decision.contradiction_status
            or card.review.contradicting_evidence_checked
            != claim_decision.contradiction_check_status
            or card.secret_exposure_level
            != claim_decision.secret_exposure_level
        ):
            return None

        effective_sensitivity = max(
            (
                expected_sensitivity,
                self._effective_claim_sensitivity(session, candidate),
                self._effective_source_sensitivity(session, source_version),
            ),
            key=_sensitivity_rank,
        )
        return card, effective_sensitivity

    def _effective_source_sensitivity(
        self,
        session: Session,
        version: SourceVersion,
    ) -> SensitiveInformationLevel:
        """Derive the current Source classification from immutable history.

        Reviews can raise, but never lower, the immutable SourceVersion base
        classification.  A classified Claim or Evidence Card also raises the
        enclosing Source version so its raw text cannot bypass child controls.
        """

        levels = [self._source_version_review_sensitivity(session, version)]
        candidates = list(
            session.scalars(
                select(ClaimCandidate).where(
                    ClaimCandidate.source_version_id == version.id
                )
            )
        )
        for candidate in candidates:
            levels.extend(
                self._claim_review_sensitivity_levels(session, candidate)
            )
        return max(levels, key=_sensitivity_rank)

    def _effective_claim_sensitivity(
        self,
        session: Session,
        candidate: ClaimCandidate,
    ) -> SensitiveInformationLevel:
        """Derive a Claim's current classification without trusting projections."""

        version = session.get(SourceVersion, candidate.source_version_id)
        if version is None:
            raise GovernanceConflictError(
                "source_version_missing",
                "The Claim's immutable Source version is unavailable.",
            )
        levels = [self._source_version_review_sensitivity(session, version)]
        levels.extend(self._claim_review_sensitivity_levels(session, candidate))
        return max(levels, key=_sensitivity_rank)

    def _source_version_review_sensitivity(
        self,
        session: Session,
        version: SourceVersion,
    ) -> SensitiveInformationLevel:
        levels = [version.sensitivity]
        decision = self._latest_source_decision(session, version.id)
        if decision is not None:
            levels.append(decision.sensitive_information_level)
        return max(levels, key=_sensitivity_rank)

    def _claim_review_sensitivity_levels(
        self,
        session: Session,
        candidate: ClaimCandidate,
    ) -> list[SensitiveInformationLevel]:
        levels: list[SensitiveInformationLevel] = []
        decision = self._latest_claim_decision(session, candidate.id)
        if decision is not None:
            levels.append(decision.sensitive_information_level)
        evidence_version = self._latest_evidence_card_version(session, candidate.id)
        if evidence_version is not None:
            try:
                card = EvidenceCard.model_validate(evidence_version.payload)
            except ValidationError:
                # A malformed persisted projection cannot safely lower access.
                levels.append(SensitiveInformationLevel.RESTRICTED)
            else:
                levels.append(card.review.sensitive_information_level)
        return levels

    def _source_state(self, session: Session, version_id: UUID) -> WorkflowStatus:
        decision = self._latest_source_decision(session, version_id)
        return (
            WorkflowStatus(decision.resulting_status.value)
            if decision is not None
            else WorkflowStatus.SUBMITTED
        )

    @staticmethod
    def _latest_source_permission_request(
        session: Session,
        version_id: UUID,
        *,
        for_update: bool = False,
    ) -> SourcePermissionRequest | None:
        statement = (
            select(SourcePermissionRequest)
            .where(SourcePermissionRequest.source_version_id == version_id)
            .order_by(SourcePermissionRequest.sequence_number.desc())
            .limit(1)
        )
        if for_update:
            statement = statement.with_for_update()
        return session.scalar(statement)

    def _permission_request_view(
        self,
        session: Session,
        row: SourcePermissionRequest,
    ) -> SourcePermissionRequestView:
        self._require_permission_request_integrity(session, row)
        submitter = UserRepository(session).get(row.submitted_by_user_id)
        if submitter is None:
            raise GovernanceConflictError(
                "permission_request_submitter_missing",
                "The permission request submitter is unavailable.",
            )
        return SourcePermissionRequestView(
            id=row.id,
            source_version_id=row.source_version_id,
            sequence=row.sequence_number,
            requested_extraction_permission=row.requested_extraction_permission,
            requested_storage_permission=row.requested_storage_permission,
            requested_scope_locators=[
                str(value) for value in row.requested_scope_locators
            ],
            rights_basis=row.rights_basis,
            rights_evidence=[str(value) for value in row.rights_evidence],
            reason=row.reason,
            submitted_by=submitter.username,
            actor_role_snapshot=[str(value) for value in row.actor_role_snapshot],
            request_checksum=row.request_checksum,
            supersedes_request_id=row.supersedes_request_id,
            created_at=_aware_utc(row.created_at),
        )

    def _require_permission_request_integrity(
        self,
        session: Session,
        row: SourcePermissionRequest,
    ) -> None:
        version = session.get(SourceVersion, row.source_version_id)
        if version is None:
            raise GovernanceConflictError(
                "source_permission_request_integrity_failed",
                "The Source permission request failed its integrity check.",
            )
        try:
            require_permission_request_integrity(row, version)
        except SourcePermissionPolicyError as exc:
            raise GovernanceConflictError(exc.code, str(exc)) from exc

    def _require_latest_permission_request_integrity(
        self,
        session: Session,
        row: SourcePermissionRequest,
    ) -> None:
        version = session.get(SourceVersion, row.source_version_id)
        if version is None:
            raise GovernanceConflictError(
                "source_permission_request_integrity_failed",
                "The Source permission request failed its integrity check.",
            )
        try:
            require_permission_request_chain_integrity(session, version, row)
        except SourcePermissionPolicyError as exc:
            raise GovernanceConflictError(exc.code, str(exc)) from exc

    @staticmethod
    def _stored_permission_request_material_payload(
        row: SourcePermissionRequest,
    ) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_PERMISSION_REQUEST_SCHEMA_VERSION,
            "source_version_id": str(row.source_version_id),
            "requested_extraction_permission": (
                row.requested_extraction_permission.value
            ),
            "requested_storage_permission": row.requested_storage_permission.value,
            "requested_scope_locators": sorted(
                str(value) for value in row.requested_scope_locators
            ),
            "rights_basis": row.rights_basis,
            "rights_evidence": sorted(str(value) for value in row.rights_evidence),
        }

    @staticmethod
    def _latest_source_decision(
        session: Session, version_id: UUID
    ) -> SourceReviewDecision | None:
        return session.scalar(
            select(SourceReviewDecision)
            .where(SourceReviewDecision.source_version_id == version_id)
            .order_by(SourceReviewDecision.sequence_number.desc())
            .limit(1)
        )

    @staticmethod
    def _latest_claim_decision(
        session: Session, claim_id: UUID
    ) -> ClaimReviewDecision | None:
        return session.scalar(
            select(ClaimReviewDecision)
            .where(ClaimReviewDecision.claim_candidate_id == claim_id)
            .order_by(ClaimReviewDecision.sequence_number.desc())
            .limit(1)
        )

    @staticmethod
    def _latest_evidence_card_version(
        session: Session, claim_id: UUID
    ) -> EvidenceCardVersion | None:
        return session.scalar(
            select(EvidenceCardVersion)
            .where(EvidenceCardVersion.claim_candidate_id == claim_id)
            .order_by(
                EvidenceCardVersion.version_number.desc(),
                EvidenceCardVersion.created_at.desc(),
            )
            .limit(1)
        )

    def _approved_source_decision(
        self, session: Session, version_id: UUID
    ) -> SourceReviewDecision:
        decision = self._latest_source_decision(session, version_id)
        if decision is None or decision.resulting_status != GovernanceWorkflowStatus.APPROVED:
            raise GovernanceConflictError(
                "source_not_approved",
                "The exact Source version is not approved for Claim extraction.",
            )
        version = session.get(SourceVersion, version_id)
        if version is None:
            raise GovernanceConflictError(
                "source_not_approved",
                "The exact Source version is unavailable.",
            )
        try:
            require_current_source_approval(session, version, decision)
        except SourcePermissionPolicyError as exc:
            raise GovernanceConflictError(exc.code, str(exc)) from exc
        return decision

    @staticmethod
    def _source_approval_is_current(
        session: Session,
        version: SourceVersion,
        decision: SourceReviewDecision,
    ) -> bool:
        """Return false for every malformed, stale, or over-broad approval."""

        try:
            require_current_source_approval(session, version, decision)
        except SourcePermissionPolicyError:
            return False
        return True

    @staticmethod
    def _next_source_decision_sequence(session: Session, version_id: UUID) -> int:
        current = session.scalar(
            select(func.max(SourceReviewDecision.sequence_number)).where(
                SourceReviewDecision.source_version_id == version_id
            )
        )
        return int(current or 0) + 1

    @staticmethod
    def _next_claim_decision_sequence(session: Session, claim_id: UUID) -> int:
        current = session.scalar(
            select(func.max(ClaimReviewDecision.sequence_number)).where(
                ClaimReviewDecision.claim_candidate_id == claim_id
            )
        )
        return int(current or 0) + 1

    def _require_permission(
        self,
        session: Session,
        actor: AuthenticatedActor,
        permission: Permission,
    ) -> tuple[AuthenticatedActor, frozenset[RoleName]]:
        return self._require_any_permission(session, actor, {permission})

    def _require_any_permission(
        self,
        session: Session,
        actor: AuthenticatedActor,
        permissions: set[Permission],
    ) -> tuple[AuthenticatedActor, frozenset[RoleName]]:
        if (
            actor.user_id is None
            or actor.session_id is None
            or actor.is_bootstrap_anonymous
        ):
            raise GovernanceAuthorizationError()
        session_record = SessionRepository(session).get(actor.session_id, for_update=True)
        now = self._now()
        if (
            session_record is None
            or session_record.user_id != actor.user_id
            or session_record.status != SessionStatus.ACTIVE
            or _aware_utc(session_record.expires_at) <= now
            or session_record.transport != actor.session_transport
        ):
            raise GovernanceAuthorizationError()
        user = UserRepository(session).get(actor.user_id, for_update=True)
        if user is None or user.status != UserStatus.ACTIVE:
            raise GovernanceAuthorizationError()
        roles = RoleRepository(session).active_names_for_user(actor.user_id)
        current = AuthenticatedActor(
            user_id=user.id,
            username=user.username,
            roles=roles,
            session_id=session_record.id,
            session_transport=session_record.transport,
            session_expires_at=_aware_utc(session_record.expires_at),
            break_glass=actor.break_glass,
        )
        if not any(has_permission(current, permission) for permission in permissions):
            raise GovernanceAuthorizationError()
        return current, roles

    def _begin_idempotency(
        self,
        session: Session,
        actor: AuthenticatedActor,
        scope: str,
        key: str,
        payload: Any,
    ):
        assert actor.user_id is not None
        try:
            record, replay = IdempotencyRepository(session).begin(
                actor_user_id=actor.user_id,
                scope=scope,
                key=key,
                request_hash=canonical_payload_hash(payload),
                now=self._now(),
            )
        except IdempotencyConflictError as exc:
            raise GovernanceConflictError(
                "idempotency_conflict", str(exc)
            ) from exc
        except IdempotencyInProgressError as exc:
            raise GovernanceConflictError(
                "idempotency_in_progress", str(exc)
            ) from exc
        except IdempotencyError as exc:
            raise GovernanceInputError(
                "invalid_idempotency_key", str(exc)
            ) from exc
        return record, replay.body if replay is not None else None

    def _ensure_sensitivity_visible(
        self,
        actor: AuthenticatedActor,
        sensitivity: SensitiveInformationLevel,
    ) -> None:
        authorization = retrieval_authorization_for_actor(actor)
        if sensitivity not in authorization.allowed_sensitive_levels:
            raise GovernanceAuthorizationError()

    def _authorized_source_replay(
        self,
        session: Session,
        actor: AuthenticatedActor,
        payload: dict[str, Any] | list[Any],
    ) -> SourceSummary:
        summary = SourceSummary.model_validate(payload)
        source = session.get(Source, summary.id)
        if source is None:
            raise GovernanceNotFoundError("source")
        for cached_version in summary.versions:
            version = session.get(SourceVersion, cached_version.id)
            if version is None or version.source_id != source.id:
                raise GovernanceNotFoundError("source")
            self._ensure_sensitivity_visible(
                actor,
                self._effective_source_sensitivity(session, version),
            )
            if cached_version.latest_permission_request_id is not None:
                cached_request = session.get(
                    SourcePermissionRequest,
                    cached_version.latest_permission_request_id,
                )
                if (
                    cached_request is None
                    or cached_request.source_version_id != version.id
                ):
                    raise GovernanceConflictError(
                        "source_permission_request_integrity_failed",
                        "The cached Source permission request is unavailable.",
                    )
                self._require_latest_permission_request_integrity(
                    session, cached_request
                )
        return summary

    def _authorized_permission_request_replay(
        self,
        session: Session,
        actor: AuthenticatedActor,
        source_id: UUID,
        version_id: UUID,
        payload: dict[str, Any] | list[Any],
    ) -> SourcePermissionRequestView:
        cached = SourcePermissionRequestView.model_validate(payload)
        row = session.get(SourcePermissionRequest, cached.id)
        version = session.get(SourceVersion, version_id)
        if (
            row is None
            or version is None
            or row.source_version_id != version_id
            or cached.source_version_id != version_id
            or version.source_id != source_id
            or row.request_checksum != cached.request_checksum
        ):
            raise GovernanceNotFoundError("source_permission_request")
        fresh = self._permission_request_view(session, row)
        if cached != fresh:
            raise GovernanceConflictError(
                "source_permission_request_integrity_failed",
                "The cached Source permission request failed its integrity check.",
            )
        self._ensure_sensitivity_visible(
            actor,
            self._effective_source_sensitivity(session, version),
        )
        return fresh

    def _authorized_claim_replay(
        self,
        session: Session,
        actor: AuthenticatedActor,
        payload: dict[str, Any] | list[Any],
    ) -> ClaimCandidateSummary:
        summary = ClaimCandidateSummary.model_validate(payload)
        candidate = session.get(ClaimCandidate, summary.id)
        if (
            candidate is None
            or candidate.source_version_id != summary.source_version_id
        ):
            raise GovernanceNotFoundError("claim")
        source_version = session.get(SourceVersion, candidate.source_version_id)
        source_decision = self._latest_source_decision(
            session, candidate.source_version_id
        )
        if (
            source_version is None
            or source_decision is None
            or source_decision.resulting_status
            != GovernanceWorkflowStatus.APPROVED
            or not self._source_approval_is_current(
                session,
                source_version,
                source_decision,
            )
        ):
            raise GovernanceNotFoundError("claim")
        self._ensure_sensitivity_visible(
            actor, self._effective_claim_sensitivity(session, candidate)
        )
        self._ensure_sensitivity_visible(
            actor, self._effective_source_sensitivity(session, source_version)
        )
        return summary

    def _authorized_claim_review_replay(
        self,
        session: Session,
        actor: AuthenticatedActor,
        payload: dict[str, Any] | list[Any],
    ) -> ClaimReviewResult:
        result = ClaimReviewResult.model_validate(payload)
        candidate = session.get(ClaimCandidate, result.claim.id)
        if (
            candidate is None
            or candidate.source_version_id != result.claim.source_version_id
        ):
            raise GovernanceNotFoundError("claim")
        source_version = session.get(SourceVersion, candidate.source_version_id)
        source_decision = self._latest_source_decision(
            session, candidate.source_version_id
        )
        if (
            source_version is None
            or source_decision is None
            or source_decision.resulting_status
            != GovernanceWorkflowStatus.APPROVED
            or not self._source_approval_is_current(
                session,
                source_version,
                source_decision,
            )
        ):
            raise GovernanceNotFoundError("claim")
        self._ensure_sensitivity_visible(
            actor, self._effective_claim_sensitivity(session, candidate)
        )
        self._ensure_sensitivity_visible(
            actor, self._effective_source_sensitivity(session, source_version)
        )
        return result

    @staticmethod
    def _require_human_reviewer(actor: AuthenticatedActor) -> None:
        try:
            require_human_identity(actor.username)
        except ValueError as exc:
            raise GovernanceAuthorizationError(
                "human_reviewer_required",
                "A named human reviewer is required.",
            ) from exc

    def _now(self) -> datetime:
        return _aware_utc(self._clock())


def _source_identity(command: SourceVersionCommand) -> tuple[str, str]:
    if command.citation.doi:
        identity = command.citation.doi
        return f"doi:{identity}", identity
    canonical_url = _canonical_url(command.citation.url)
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    return f"url-sha256:{digest}", canonical_url


def _normalized_requested_extraction_permission(
    value: Any,
) -> ExtractionPermission:
    try:
        return ExtractionPermission(str(value))
    except ValueError:
        return ExtractionPermission.NONE


def _normalized_requested_storage_permission(value: Any) -> StoragePermission:
    try:
        return StoragePermission(str(value))
    except ValueError:
        return StoragePermission.NONE


def _normalized_requested_scope(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _source_idempotency_payload(command: SourceVersionCommand) -> dict[str, Any]:
    """Remove server-generated provenance timestamps from retry identity."""

    payload = command.model_dump(mode="json")
    serialized = payload.get("citation", {}).get("provenance", [])
    for provenance_model, provenance_payload in zip(
        command.citation.provenance,
        serialized,
        strict=True,
    ):
        if "retrieved_at" not in provenance_model.model_fields_set:
            provenance_payload.pop("retrieved_at", None)
    return payload


def _permission_request_material_payload(
    command: SourcePermissionRequestCommand,
) -> dict[str, Any]:
    """Fields whose change justifies a new append-only permission request."""

    return {
        "schema_version": command.schema_version,
        "source_version_id": str(command.source_version_id),
        "requested_extraction_permission": (
            command.requested_extraction_permission.value
        ),
        "requested_storage_permission": command.requested_storage_permission.value,
        "requested_scope_locators": sorted(command.requested_scope_locators),
        "rights_basis": command.rights_basis,
        "rights_evidence": sorted(command.rights_evidence),
    }


def _claim_review_idempotency_payload(
    command: ClaimReviewCommand,
) -> dict[str, Any]:
    payload = command.model_dump(mode="json")
    confidence = payload.get("confidence")
    if (
        isinstance(confidence, dict)
        and command.confidence is not None
        and "assessed_at" not in command.confidence.model_fields_set
    ):
        confidence.pop("assessed_at", None)
    return payload


def _canonical_url(value: str) -> str:
    normalized = value.strip()
    parsed = urlsplit(normalized)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise GovernanceInputError("invalid_source_url", "The Source URL is invalid.")
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    ).rstrip("/")


def _canonical_doi(value: str) -> str:
    normalized = value.strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
    return normalized


def _checked_locator_matches(version: SourceVersion, value: str) -> bool:
    """Require a verification observation to point back to this citation."""

    locator = value.strip()
    citation = version.citation_metadata
    try:
        if _canonical_url(locator) == _canonical_url(str(citation.get("url") or "")):
            return True
    except GovernanceInputError:
        return False
    doi = str(citation.get("doi") or "").strip().casefold()
    return bool(doi and doi in locator.casefold())


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _decision_status(decision: ReviewDecision) -> WorkflowStatus:
    return (
        WorkflowStatus.APPROVED
        if decision == ReviewDecision.APPROVE
        else WorkflowStatus.REJECTED
    )


def _orm_status(status: WorkflowStatus) -> GovernanceWorkflowStatus:
    return GovernanceWorkflowStatus(status.value)


def _require_transition(
    object_name: str,
    current: WorkflowStatus,
    target: WorkflowStatus,
) -> None:
    allowed = {
        WorkflowStatus.SUBMITTED: {
            WorkflowStatus.APPROVED,
            WorkflowStatus.REJECTED,
        }
    }
    if target not in allowed.get(current, set()):
        raise GovernanceConflictError(
            "invalid_state_transition",
            f"Invalid {object_name} transition from {current.value} to {target.value}.",
        )


def _storage_rank(permission: StoragePermission) -> int:
    return {
        StoragePermission.NONE: 0,
        StoragePermission.DERIVED_KNOWLEDGE_ONLY: 1,
        StoragePermission.DERIVED_WITH_SHORT_EXCERPT: 2,
    }[permission]


def _claim_eligibility_rank(eligibility: ClaimEligibility) -> int:
    return {
        ClaimEligibility.INELIGIBLE: 0,
        ClaimEligibility.NOT_ASSESSED: 1,
        ClaimEligibility.ELIGIBLE_WITH_LIMITS: 2,
        ClaimEligibility.ELIGIBLE: 3,
    }[eligibility]


def _extraction_rank(permission: ExtractionPermission) -> int:
    return {
        ExtractionPermission.NONE: 0,
        ExtractionPermission.METADATA_ONLY: 1,
        ExtractionPermission.SELECTED_SECTIONS: 2,
        ExtractionPermission.FULL_TEXT: 3,
    }[permission]


def _sensitivity_rank(level: SensitiveInformationLevel) -> int:
    return {
        SensitiveInformationLevel.PUBLIC: 0,
        SensitiveInformationLevel.CONTROLLED: 1,
        SensitiveInformationLevel.SECRET_METHOD: 2,
        SensitiveInformationLevel.RESTRICTED: 3,
    }[level]


def _sensitivity_for_exposure(
    exposure: SecretExposureLevel,
) -> SensitiveInformationLevel:
    return {
        SecretExposureLevel.NONE: SensitiveInformationLevel.PUBLIC,
        SecretExposureLevel.GENERAL_PRINCIPLE: SensitiveInformationLevel.CONTROLLED,
        SecretExposureLevel.METHOD_DETAIL: SensitiveInformationLevel.SECRET_METHOD,
        SecretExposureLevel.OPERATIONAL_SECRET: SensitiveInformationLevel.RESTRICTED,
    }[exposure]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "GovernanceAuthorizationError",
    "GovernanceConflictError",
    "GovernanceInputError",
    "GovernanceNotFoundError",
    "GovernanceServiceError",
    "ProductionGovernanceService",
]
