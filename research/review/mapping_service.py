"""Transactional Production Mapping governance service.

Mapping proposals are immutable inputs over exact, approved Evidence Card
versions.  Approval reruns deterministic semantic and authorization gates and
atomically appends a human decision plus a versioned knowledge artifact.  This
module deliberately stops before Storage Manifest authorization and Qdrant.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from knowledge.evidence import (
    ConfidenceAssessment,
    ConfidenceLabel,
    EvidenceCard,
    KnowledgeOrigin,
    MagicDomain,
)
from knowledge.governance import (
    ClaimEligibility,
    ReviewStatus,
    SensitiveInformationLevel,
    StoragePermission,
    require_human_identity,
)
from knowledge.models import (
    EntityType,
    KnowledgeEntity,
    KnowledgeRelationship,
    stable_entity_id,
)
from knowledge.projections import (
    KnowledgeNodeVersion,
    KnowledgeRelationshipAssertion,
)
from persistence import (
    AuditEventRepository,
    CanonicalEntityStatus,
    ClaimCandidate,
    ClaimReviewDecision,
    EvidenceCardVersion,
    GovernanceWorkflowStatus,
    KnowledgeEntityRecord,
    KnowledgeNodeVersionRecord,
    KnowledgeRelationshipAssertionRecord,
    KnowledgeRelationshipRecord,
    MappingProposalEvidence,
    MappingProposalRecord,
    MappingReviewDecision,
    MappingValidationPhase,
    MappingValidationRun,
    ReviewDecisionKind,
    RoleName,
    SessionStatus,
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
from research.extraction.models import (
    ExtractedEntity,
    RelationshipMappingProposal,
)
from research.extraction.relationship_validation import (
    validate_relationship_entailment,
)
from research.extraction.semantic import (
    canonical_entity_key,
    validate_extracted_entity,
)
from research.review.mapping_models import (
    ENTITY_VALIDATION_RULE_VERSION,
    MAPPING_REVIEW_POLICY_VERSION,
    RELATIONSHIP_ENTAILMENT_RULE_VERSION,
    CanonicalEntitySummary,
    CanonicalResolution,
    EntityMappingCommand,
    KnowledgeNodeVersionView,
    KnowledgeRelationshipAssertionView,
    MappingProposalDetail,
    MappingProposalKind,
    MappingProposalSummary,
    MappingReviewCommand,
    MappingReviewResult,
    RelationshipMappingCommand,
    ValidationRunView,
)
from research.review.workflow_models import ReviewDecision, WorkflowStatus
from research.review.source_permission_policy import (
    SourcePermissionPolicyError,
    require_current_source_approval,
)
from research.review.workflow_service import (
    GovernanceAuthorizationError,
    GovernanceConflictError,
    GovernanceInputError,
    GovernanceNotFoundError,
)
from security.policy import (
    AuthenticatedActor,
    Permission,
    has_permission,
    retrieval_authorization_for_actor,
)
from security.repositories import RoleRepository, SessionRepository, UserRepository


Clock = Callable[[], datetime]
AuditRepositoryFactory = Callable[[Session], AuditEventRepository]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ProductionMappingService:
    """Own Mapping submission, review, and immutable knowledge versioning."""

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

    def submit_entity(
        self,
        actor: AuthenticatedActor,
        command: EntityMappingCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> MappingProposalSummary:
        return self._submit(
            actor,
            MappingProposalKind.ENTITY,
            command,
            idempotency_key=idempotency_key,
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def submit_relationship(
        self,
        actor: AuthenticatedActor,
        command: RelationshipMappingCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> MappingProposalSummary:
        return self._submit(
            actor,
            MappingProposalKind.RELATIONSHIP,
            command,
            idempotency_key=idempotency_key,
            request_id=request_id,
            correlation_id=correlation_id,
        )

    def list_mappings(
        self,
        actor: AuthenticatedActor,
        *,
        status: WorkflowStatus | None = None,
        kind: MappingProposalKind | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MappingProposalSummary]:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_permission(
                unit.session, actor, Permission.MAPPING_REVIEW
            )
            statement = select(MappingProposalRecord)
            if status is not None:
                statement = statement.where(
                    MappingProposalRecord.status
                    == GovernanceWorkflowStatus(status.value)
                )
            if kind is not None:
                statement = statement.where(MappingProposalRecord.proposal_kind == kind)
            allowed = retrieval_authorization_for_actor(current).allowed_sensitive_levels
            statement = (
                statement.where(MappingProposalRecord.sensitivity.in_(allowed))
                .order_by(
                    MappingProposalRecord.submitted_at.asc(),
                    MappingProposalRecord.id.asc(),
                )
                .limit(limit)
                .offset(offset)
            )
            return [self._summary(row) for row in unit.session.scalars(statement)]

    def get_mapping(
        self, actor: AuthenticatedActor, mapping_id: UUID
    ) -> MappingProposalDetail:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_permission(
                unit.session, actor, Permission.MAPPING_REVIEW
            )
            row = unit.session.get(MappingProposalRecord, mapping_id)
            if row is None:
                raise GovernanceNotFoundError("mapping")
            self._ensure_sensitivity_visible(current, row.sensitivity)
            return self._detail(unit.session, row)

    def list_canonical_entities(
        self,
        actor: AuthenticatedActor,
        *,
        query: str | None = None,
        entity_type: EntityType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CanonicalEntitySummary]:
        if not 1 <= limit <= 100 or offset < 0:
            raise GovernanceInputError(
                "invalid_pagination", "Pagination parameters are invalid."
            )
        normalized_query = " ".join((query or "").split()).casefold()
        if len(normalized_query) > 200:
            raise GovernanceInputError(
                "invalid_entity_query", "The canonical entity query is too long."
            )
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_permission(
                unit.session, actor, Permission.MAPPING_REVIEW
            )
            allowed = set(
                retrieval_authorization_for_actor(current).allowed_sensitive_levels
            )
            statement = select(KnowledgeEntityRecord).where(
                KnowledgeEntityRecord.status == CanonicalEntityStatus.ACTIVE,
                KnowledgeEntityRecord.merged_into_entity_id.is_(None),
            )
            if entity_type is not None:
                statement = statement.where(
                    KnowledgeEntityRecord.entity_type == entity_type
                )
            rows = list(
                unit.session.scalars(
                    statement.order_by(
                        KnowledgeEntityRecord.canonical_name.asc(),
                        KnowledgeEntityRecord.id.asc(),
                    )
                )
            )
            visible: list[CanonicalEntitySummary] = []
            for row in rows:
                latest = unit.session.scalar(
                    select(KnowledgeNodeVersionRecord)
                    .where(KnowledgeNodeVersionRecord.entity_id == row.id)
                    .order_by(
                        KnowledgeNodeVersionRecord.version_number.desc(),
                        KnowledgeNodeVersionRecord.created_at.desc(),
                    )
                    .limit(1)
                )
                if latest is None:
                    continue
                proposal = unit.session.get(
                    MappingProposalRecord, latest.mapping_proposal_id
                )
                if proposal is None or proposal.sensitivity not in allowed:
                    continue
                try:
                    self._require_current_node_approval(
                        unit.session, latest, proposal, current
                    )
                except (
                    GovernanceAuthorizationError,
                    GovernanceConflictError,
                    GovernanceInputError,
                    GovernanceNotFoundError,
                    ValidationError,
                    ValueError,
                ):
                    continue
                searchable = " ".join(
                    [row.canonical_name, row.canonical_key, *row.aliases]
                ).casefold()
                if normalized_query and normalized_query not in searchable:
                    continue
                visible.append(
                    CanonicalEntitySummary(
                        id=row.id,
                        entity_type=row.entity_type,
                        canonical_name=row.canonical_name,
                        canonical_key=row.canonical_key,
                        aliases=list(row.aliases),
                        description=row.description or "",
                        latest_knowledge_node_version_id=latest.id,
                        latest_version=latest.version_number,
                    )
                )
            return visible[offset : offset + limit]

    def review_mapping(
        self,
        actor: AuthenticatedActor,
        mapping_id: UUID,
        command: MappingReviewCommand,
        *,
        idempotency_key: str,
        request_id: str | None = None,
        correlation_id: str | None = None,
    ) -> MappingReviewResult:
        request_payload = {
            "mapping_id": str(mapping_id),
            "review": command.model_dump(mode="json"),
        }
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_permission(
                unit.session, actor, Permission.MAPPING_REVIEW
            )
            human = self._human(current.username)
            idempotency, replay = self._begin_idempotency(
                unit.session,
                current,
                f"mappings.{mapping_id}.review",
                idempotency_key,
                request_payload,
            )
            if replay is not None:
                result = MappingReviewResult.model_validate(replay)
                row = unit.session.get(MappingProposalRecord, mapping_id)
                if row is None:
                    raise GovernanceNotFoundError("mapping")
                self._ensure_sensitivity_visible(current, row.sensitivity)
                return result

            proposal = unit.session.scalar(
                select(MappingProposalRecord)
                .where(MappingProposalRecord.id == mapping_id)
                .with_for_update()
            )
            if proposal is None:
                raise GovernanceNotFoundError("mapping")
            self._ensure_sensitivity_visible(current, proposal.sensitivity)
            if proposal.proposer_user_id == current.user_id:
                raise GovernanceAuthorizationError(
                    "mapping_self_review_forbidden",
                    "A Mapping proposer cannot review their own proposal.",
                )
            if proposal.status != GovernanceWorkflowStatus.SUBMITTED:
                raise GovernanceConflictError(
                    "mapping_not_pending",
                    "Only a submitted Mapping proposal can be reviewed.",
                )

            sequence = self._next_decision_sequence(unit.session, proposal.id)
            if command.decision == ReviewDecision.REJECT:
                self._validate_rejection_shape(command)
                decision = self._new_decision(
                    proposal=proposal,
                    sequence=sequence,
                    reviewer=current,
                    roles=roles,
                    command=command,
                    status=GovernanceWorkflowStatus.REJECTED,
                )
                unit.session.add(decision)
                unit.flush()
                proposal.status = GovernanceWorkflowStatus.REJECTED
                proposal.updated_at = self._now()
                unit.flush()
                result = MappingReviewResult(mapping=self._summary(proposal))
                self._audit_factory(unit.session).append(
                    event_type="mapping_rejected",
                    actor_user_id=current.user_id,
                    actor_role_snapshot=roles,
                    object_type="mapping_proposal",
                    object_id=str(proposal.id),
                    previous_state={"status": WorkflowStatus.SUBMITTED.value},
                    new_state={"status": WorkflowStatus.REJECTED.value},
                    reason=command.reason,
                    request_id=request_id,
                    correlation_id=correlation_id,
                    payload_checksum=decision.decision_checksum,
                )
                self._complete_idempotency(unit.session, idempotency, result, 200)
                unit.commit()
                return result

            assert command.decision == ReviewDecision.APPROVE
            confidence = self._review_confidence(command, human)
            evidence_rows, cards = self._load_evidence_for_proposal(
                unit.session, proposal, current, lock=True
            )
            parsed = self._parse(proposal)
            if proposal.proposal_kind == MappingProposalKind.RELATIONSHIP and (
                command.canonical_resolution is not None
                or command.canonical_entity_id is not None
            ):
                raise GovernanceInputError(
                    "relationship_resolution_forbidden",
                    "Relationship approval cannot carry canonical entity resolution.",
                )
            validation_results = self._validate_mapping(
                unit.session, proposal.proposal_kind, parsed, cards
            )
            validation = self._validation_run(
                proposal,
                current,
                evidence_rows,
                validation_results,
            )
            unit.session.add(validation)
            unit.flush()

            if proposal.proposal_kind == MappingProposalKind.ENTITY:
                if not isinstance(parsed, EntityMappingCommand):
                    raise GovernanceConflictError(
                        "mapping_payload_kind_mismatch",
                        "The persisted Mapping payload is inconsistent.",
                    )
                entity, version, supersedes = self._resolve_entity_and_version(
                    unit.session, parsed, command, current
                )
                node = self._build_node(
                    proposal,
                    parsed,
                    entity,
                    version,
                    supersedes,
                    cards,
                    confidence,
                    human,
                )
                artifact_id = UUID(node.id)
                artifact_version = node.version
                decision = self._new_decision(
                    proposal=proposal,
                    sequence=sequence,
                    reviewer=current,
                    roles=roles,
                    command=command,
                    status=GovernanceWorkflowStatus.APPROVED,
                    validation=validation,
                    artifact_id=artifact_id,
                    artifact_version=artifact_version,
                )
                unit.session.add(decision)
                unit.flush()
                unit.session.add(
                    KnowledgeNodeVersionRecord(
                        id=artifact_id,
                        entity_id=UUID(node.entity.id),
                        version_number=node.version,
                        schema_version=node.schema_version,
                        payload=node.model_dump(mode="json"),
                        payload_checksum=canonical_payload_hash(
                            node.model_dump(mode="json")
                        ),
                        mapping_proposal_id=proposal.id,
                        mapping_review_decision_id=decision.id,
                        supersedes_version_id=(
                            supersedes.id if supersedes is not None else None
                        ),
                        created_by_user_id=current.user_id,
                        created_at=self._now(),
                    )
                )
                unit.flush()
                result = MappingReviewResult(
                    mapping=self._approve_envelope(
                        unit.session, proposal, artifact_id, artifact_version
                    ),
                    knowledge_node_version_id=artifact_id,
                )
                artifact_type = "knowledge_node_version"
            else:
                if not isinstance(parsed, RelationshipMappingCommand):
                    raise GovernanceConflictError(
                        "mapping_payload_kind_mismatch",
                        "The persisted Mapping payload is inconsistent.",
                    )
                relationship, assertion, supersedes = self._build_relationship_assertion(
                    unit.session,
                    proposal,
                    parsed,
                    cards,
                    confidence,
                    human,
                    current,
                )
                artifact_id = UUID(assertion.id)
                artifact_version = assertion.version
                decision = self._new_decision(
                    proposal=proposal,
                    sequence=sequence,
                    reviewer=current,
                    roles=roles,
                    command=command,
                    status=GovernanceWorkflowStatus.APPROVED,
                    validation=validation,
                    artifact_id=artifact_id,
                    artifact_version=artifact_version,
                )
                unit.session.add(decision)
                unit.flush()
                unit.session.add(
                    KnowledgeRelationshipAssertionRecord(
                        id=artifact_id,
                        relationship_id=UUID(relationship.id),
                        version_number=assertion.version,
                        schema_version=assertion.schema_version,
                        payload=assertion.model_dump(mode="json"),
                        projection_context={
                            "domains": [value.value for value in parsed.domains],
                            "ontology_paths": parsed.ontology_paths,
                            "topic_tags": parsed.topic_tags,
                        },
                        payload_checksum=canonical_payload_hash(
                            assertion.model_dump(mode="json")
                        ),
                        mapping_proposal_id=proposal.id,
                        mapping_review_decision_id=decision.id,
                        supersedes_assertion_id=(
                            supersedes.id if supersedes is not None else None
                        ),
                        created_by_user_id=current.user_id,
                        created_at=self._now(),
                    )
                )
                unit.flush()
                result = MappingReviewResult(
                    mapping=self._approve_envelope(
                        unit.session, proposal, artifact_id, artifact_version
                    ),
                    relationship_assertion_id=artifact_id,
                )
                artifact_type = "relationship_assertion"

            self._audit_factory(unit.session).append(
                event_type="mapping_approved",
                actor_user_id=current.user_id,
                actor_role_snapshot=roles,
                object_type="mapping_proposal",
                object_id=str(proposal.id),
                previous_state={"status": WorkflowStatus.SUBMITTED.value},
                new_state={
                    "status": WorkflowStatus.APPROVED.value,
                    "artifact_type": artifact_type,
                    "artifact_id": str(artifact_id),
                    "artifact_version": artifact_version,
                    "validation_run_id": str(validation.id),
                    "validation_rule_version": validation.rule_version,
                },
                reason=command.reason,
                request_id=request_id,
                correlation_id=correlation_id,
                payload_checksum=decision.decision_checksum,
            )
            self._complete_idempotency(unit.session, idempotency, result, 200)
            unit.commit()
            return result

    def get_node_version(
        self, actor: AuthenticatedActor, version_id: UUID
    ) -> KnowledgeNodeVersionView:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_permission(
                unit.session, actor, Permission.MAPPING_REVIEW
            )
            row = unit.session.get(KnowledgeNodeVersionRecord, version_id)
            if row is None:
                raise GovernanceNotFoundError("knowledge_node_version")
            proposal = unit.session.get(MappingProposalRecord, row.mapping_proposal_id)
            if proposal is None:
                raise GovernanceNotFoundError("knowledge_node_version")
            return self._require_current_node_approval(
                unit.session, row, proposal, current
            )

    def get_relationship_assertion(
        self, actor: AuthenticatedActor, assertion_id: UUID
    ) -> KnowledgeRelationshipAssertionView:
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, _ = self._require_permission(
                unit.session, actor, Permission.MAPPING_REVIEW
            )
            row = unit.session.get(KnowledgeRelationshipAssertionRecord, assertion_id)
            if row is None:
                raise GovernanceNotFoundError("relationship_assertion")
            proposal = unit.session.get(MappingProposalRecord, row.mapping_proposal_id)
            if proposal is None:
                raise GovernanceNotFoundError("relationship_assertion")
            return self._require_current_assertion_approval(
                unit.session, row, proposal, current
            )

    def _submit(
        self,
        actor: AuthenticatedActor,
        kind: MappingProposalKind,
        command: EntityMappingCommand | RelationshipMappingCommand,
        *,
        idempotency_key: str,
        request_id: str | None,
        correlation_id: str | None,
    ) -> MappingProposalSummary:
        payload = command.model_dump(mode="json")
        with SqlAlchemyUnitOfWork(self._session_factory) as unit:
            current, roles = self._require_permission(
                unit.session, actor, Permission.MAPPING_SUBMIT
            )
            idempotency, replay = self._begin_idempotency(
                unit.session,
                current,
                f"mappings.{kind.value}.submit",
                idempotency_key,
                payload,
            )
            if replay is not None:
                summary = MappingProposalSummary.model_validate(replay)
                row = unit.session.get(MappingProposalRecord, summary.id)
                if row is None:
                    raise GovernanceNotFoundError("mapping")
                self._ensure_sensitivity_visible(current, row.sensitivity)
                return summary

            evidence_rows, cards = self._load_evidence(
                unit.session,
                command.supporting_evidence_version_ids,
                current,
                lock=False,
            )
            validation = self._validate_mapping(unit.session, kind, command, cards)
            sensitivity = self._maximum_sensitivity(cards)
            checksum = canonical_payload_hash(payload)
            duplicate = unit.session.scalar(
                select(MappingProposalRecord).where(
                    MappingProposalRecord.proposal_kind == kind,
                    MappingProposalRecord.proposal_checksum == checksum,
                )
            )
            if duplicate is not None:
                summary = self._summary(duplicate)
                self._complete_idempotency(unit.session, idempotency, summary, 200)
                unit.commit()
                return summary

            subject_entity_id = (
                UUID(command.entity.id)
                if isinstance(command, EntityMappingCommand)
                else None
            )
            row = MappingProposalRecord(
                proposal_kind=kind,
                schema_version=command.schema_version,
                validation_rule_version=self._rule_version(kind),
                payload=payload,
                proposal_checksum=checksum,
                subject_entity_id=subject_entity_id,
                sensitivity=sensitivity,
                proposer_user_id=current.user_id,
                proposer_run_id=command.proposer_run_id,
                status=GovernanceWorkflowStatus.SUBMITTED,
                supersedes_proposal_id=command.supersedes_proposal_id,
                submitted_at=self._now(),
                updated_at=self._now(),
            )
            unit.session.add(row)
            unit.flush()
            for evidence in evidence_rows:
                unit.session.add(
                    MappingProposalEvidence(
                        mapping_proposal_id=row.id,
                        evidence_card_version_id=evidence.id,
                        evidence_domain_object_id=evidence.domain_object_id,
                        evidence_payload_checksum=evidence.payload_checksum,
                        created_at=self._now(),
                    )
                )
            unit.flush()
            summary = self._summary(row)
            self._audit_factory(unit.session).append(
                event_type=f"{kind.value}_mapping_submitted",
                actor_user_id=current.user_id,
                actor_role_snapshot=roles,
                object_type="mapping_proposal",
                object_id=str(row.id),
                new_state={
                    "status": WorkflowStatus.SUBMITTED.value,
                    "kind": kind.value,
                    "supporting_evidence_versions": [
                        str(value.id) for value in evidence_rows
                    ],
                    "validation": validation,
                    "validation_rule_version": row.validation_rule_version,
                    "sensitivity": sensitivity.value,
                },
                reason="Submit an immutable evidence-backed Mapping proposal.",
                request_id=request_id,
                correlation_id=correlation_id,
                payload_checksum=checksum,
            )
            self._complete_idempotency(unit.session, idempotency, summary, 201)
            unit.commit()
            return summary

    def _load_evidence_for_proposal(
        self,
        session: Session,
        proposal: MappingProposalRecord,
        actor: AuthenticatedActor,
        *,
        lock: bool,
    ) -> tuple[list[EvidenceCardVersion], list[EvidenceCard]]:
        links = list(
            session.scalars(
                select(MappingProposalEvidence)
                .where(MappingProposalEvidence.mapping_proposal_id == proposal.id)
                .order_by(MappingProposalEvidence.evidence_card_version_id.asc())
            )
        )
        parsed = self._parse(proposal)
        expected = parsed.supporting_evidence_version_ids
        if {link.evidence_card_version_id for link in links} != set(expected):
            raise GovernanceConflictError(
                "mapping_evidence_binding_changed",
                "The Mapping evidence-version binding is inconsistent.",
            )
        rows, cards = self._load_evidence(session, expected, actor, lock=lock)
        by_id = {row.id: row for row in rows}
        for link in links:
            row = by_id[link.evidence_card_version_id]
            if (
                link.evidence_domain_object_id != row.domain_object_id
                or link.evidence_payload_checksum != row.payload_checksum
            ):
                raise GovernanceConflictError(
                    "mapping_evidence_snapshot_changed",
                    "A Mapping evidence snapshot no longer matches its immutable version.",
                )
        if self._maximum_sensitivity(cards) != proposal.sensitivity:
            raise GovernanceConflictError(
                "mapping_sensitivity_changed",
                "The Mapping sensitivity classification is inconsistent.",
            )
        return rows, cards

    def _require_current_node_approval(
        self,
        session: Session,
        row: KnowledgeNodeVersionRecord,
        proposal: MappingProposalRecord,
        actor: AuthenticatedActor,
    ) -> KnowledgeNodeVersionView:
        self._ensure_sensitivity_visible(actor, proposal.sensitivity)
        view = self._node_view(row)
        decision = self._latest_mapping_decision(session, proposal.id)
        latest = session.scalar(
            select(KnowledgeNodeVersionRecord)
            .where(KnowledgeNodeVersionRecord.entity_id == row.entity_id)
            .order_by(
                KnowledgeNodeVersionRecord.version_number.desc(),
                KnowledgeNodeVersionRecord.created_at.desc(),
                KnowledgeNodeVersionRecord.id.desc(),
            )
            .limit(1)
        )
        if (
            proposal.proposal_kind != MappingProposalKind.ENTITY
            or proposal.status != GovernanceWorkflowStatus.APPROVED
            or proposal.approved_artifact_id != UUID(view.node.id)
            or proposal.approved_artifact_version != view.node.version
            or row.entity_id != UUID(view.node.entity.id)
            or row.version_number != view.node.version
            or decision is None
            or decision.resulting_status != GovernanceWorkflowStatus.APPROVED
            or decision.id != row.mapping_review_decision_id
            or decision.approved_artifact_id != proposal.approved_artifact_id
            or decision.approved_artifact_version
            != proposal.approved_artifact_version
            or latest is None
            or latest.id != row.id
        ):
            raise GovernanceConflictError(
                "knowledge_node_approval_stale",
                "The Knowledge Node no longer has a current approval.",
            )
        self._require_current_proposal_dependencies(
            session, proposal, actor
        )
        return view

    def _require_current_assertion_approval(
        self,
        session: Session,
        row: KnowledgeRelationshipAssertionRecord,
        proposal: MappingProposalRecord,
        actor: AuthenticatedActor,
    ) -> KnowledgeRelationshipAssertionView:
        self._ensure_sensitivity_visible(actor, proposal.sensitivity)
        view = self._assertion_view(row)
        decision = self._latest_mapping_decision(session, proposal.id)
        latest = session.scalar(
            select(KnowledgeRelationshipAssertionRecord)
            .where(
                KnowledgeRelationshipAssertionRecord.relationship_id
                == row.relationship_id
            )
            .order_by(
                KnowledgeRelationshipAssertionRecord.version_number.desc(),
                KnowledgeRelationshipAssertionRecord.created_at.desc(),
                KnowledgeRelationshipAssertionRecord.id.desc(),
            )
            .limit(1)
        )
        if (
            proposal.proposal_kind != MappingProposalKind.RELATIONSHIP
            or proposal.status != GovernanceWorkflowStatus.APPROVED
            or proposal.approved_artifact_id != UUID(view.assertion.id)
            or proposal.approved_artifact_version != view.assertion.version
            or row.relationship_id
            != UUID(view.assertion.relationship.id)
            or row.version_number != view.assertion.version
            or decision is None
            or decision.resulting_status != GovernanceWorkflowStatus.APPROVED
            or decision.id != row.mapping_review_decision_id
            or decision.approved_artifact_id != proposal.approved_artifact_id
            or decision.approved_artifact_version
            != proposal.approved_artifact_version
            or latest is None
            or latest.id != row.id
        ):
            raise GovernanceConflictError(
                "relationship_approval_stale",
                "The Relationship assertion no longer has a current approval.",
            )
        self._require_current_proposal_dependencies(
            session, proposal, actor
        )
        return view

    def _require_current_proposal_dependencies(
        self,
        session: Session,
        proposal: MappingProposalRecord,
        actor: AuthenticatedActor,
    ) -> tuple[list[EvidenceCardVersion], list[EvidenceCard]]:
        evidence_rows, cards = self._load_evidence_for_proposal(
            session, proposal, actor, lock=False
        )
        allowed = set(
            retrieval_authorization_for_actor(actor).allowed_sensitive_levels
        )
        levels = {proposal.sensitivity}
        for evidence_row in evidence_rows:
            levels.add(
                self._effective_source_sensitivity(
                    session, evidence_row.source_version_id
                )
            )
            claim_decision = self._latest_claim_decision(
                session, evidence_row.claim_candidate_id
            )
            if claim_decision is not None:
                levels.add(claim_decision.sensitive_information_level)
        if not levels.issubset(allowed):
            raise GovernanceAuthorizationError()
        return evidence_rows, cards

    def _load_evidence(
        self,
        session: Session,
        ids: Sequence[UUID],
        actor: AuthenticatedActor,
        *,
        lock: bool,
    ) -> tuple[list[EvidenceCardVersion], list[EvidenceCard]]:
        statement = select(EvidenceCardVersion).where(EvidenceCardVersion.id.in_(ids))
        if lock:
            statement = statement.with_for_update()
        found = {row.id: row for row in session.scalars(statement)}
        if set(found) != set(ids):
            raise GovernanceInputError(
                "evidence_version_not_found",
                "Every Mapping support reference must identify an Evidence Card version.",
            )
        rows = [found[value] for value in ids]
        cards: list[EvidenceCard] = []
        for row in rows:
            if canonical_payload_hash(row.payload) != row.payload_checksum:
                raise GovernanceConflictError(
                    "evidence_checksum_mismatch",
                    "An Evidence Card version failed its integrity check.",
                )
            try:
                card = EvidenceCard.model_validate(row.payload)
            except ValidationError as exc:
                raise GovernanceConflictError(
                    "evidence_payload_invalid",
                    "An Evidence Card version is no longer schema-valid.",
                ) from exc
            if (
                UUID(card.id) != row.domain_object_id
                or card.version != row.version_number
                or card.schema_version != row.domain_schema_version
                or UUID(card.source.source_id) != row.source_id
                or UUID(card.source.source_version_id) != row.source_version_id
                or UUID(card.source.citation_id) != row.citation_id
                or UUID(card.canonical_claim_id) != row.canonical_claim_id
            ):
                raise GovernanceConflictError(
                    "evidence_identity_mismatch",
                    "An Evidence Card version has inconsistent persisted identity metadata.",
                )
            candidate = session.get(ClaimCandidate, row.claim_candidate_id)
            latest_claim = session.scalar(
                select(ClaimReviewDecision)
                .where(ClaimReviewDecision.claim_candidate_id == row.claim_candidate_id)
                .order_by(ClaimReviewDecision.sequence_number.desc())
                .limit(1)
            )
            latest_source = session.scalar(
                select(SourceReviewDecision)
                .where(SourceReviewDecision.source_version_id == row.source_version_id)
                .order_by(SourceReviewDecision.sequence_number.desc())
                .limit(1)
            )
            if (
                candidate is None
                or candidate.status != GovernanceWorkflowStatus.APPROVED
                or not candidate.evidence_card_eligible
                or latest_claim is None
                or latest_claim.id != row.claim_review_decision_id
                or latest_claim.resulting_status != GovernanceWorkflowStatus.APPROVED
                or latest_source is None
                or latest_source.resulting_status != GovernanceWorkflowStatus.APPROVED
                or not card.review.approved
                or card.review.review_status not in {ReviewStatus.APPROVED, ReviewStatus.INGESTED}
                or not card.review.claim_eligibility.allows_projection
                or not card.review.storage_permission.allows_projection
                or not card.review.contradicting_evidence_checked.completed
            ):
                raise GovernanceConflictError(
                    "evidence_not_currently_eligible",
                    "Mapping requires exact, current, approved Evidence Card versions.",
                )
            source_version = session.get(SourceVersion, row.source_version_id)
            if source_version is None:
                raise GovernanceConflictError(
                    "source_version_missing",
                    "The exact Source version supporting Evidence is unavailable.",
                )
            try:
                require_current_source_approval(
                    session,
                    source_version,
                    latest_source,
                )
            except SourcePermissionPolicyError as exc:
                raise GovernanceConflictError(exc.code, str(exc)) from exc
            if (
                latest_claim.knowledge_origin != card.knowledge_origin
                or latest_claim.sensitive_information_level
                != card.review.sensitive_information_level
                or latest_claim.storage_permission != card.review.storage_permission
                or latest_claim.claim_eligibility != card.review.claim_eligibility
            ):
                raise GovernanceConflictError(
                    "evidence_governance_mismatch",
                    "Evidence Card governance fields do not match their approval decision.",
                )
            self._validate_current_source_policy(
                card,
                latest_claim,
                latest_source,
            )
            self._ensure_sensitivity_visible(
                actor, card.review.sensitive_information_level
            )
            cards.append(card)
        return rows, cards

    def _validate_mapping(
        self,
        session: Session,
        kind: MappingProposalKind,
        command: EntityMappingCommand | RelationshipMappingCommand,
        cards: list[EvidenceCard],
    ) -> dict[str, Any]:
        if not cards:
            raise GovernanceInputError(
                "mapping_evidence_required", "A Mapping proposal requires evidence."
            )
        origins = {card.knowledge_origin for card in cards}
        if origins != {command.knowledge_origin}:
            raise GovernanceInputError(
                "mapping_origin_mismatch",
                "A Mapping proposal cannot blend or relabel knowledge origins.",
            )
        allowed_domains = {domain for card in cards for domain in card.applicable_domain}
        if not set(command.domains).issubset(allowed_domains):
            raise GovernanceInputError(
                "mapping_domain_unsupported",
                "Mapping domains must be supported by the linked Evidence Cards.",
            )
        locator_match = any(
            _normalized(command.evidence_excerpt) == _normalized(card.evidence_excerpt)
            and _normalized(command.source_locator)
            == _normalized(card.locator.source_locator)
            for card in cards
        )
        if not locator_match:
            raise GovernanceInputError(
                "mapping_locator_unsupported",
                "The Mapping excerpt and locator must match a supporting Evidence Card.",
            )

        if kind == MappingProposalKind.ENTITY:
            if not isinstance(command, EntityMappingCommand):
                raise GovernanceInputError(
                    "mapping_kind_mismatch", "An entity payload is required."
                )
            try:
                entity_id = UUID(command.entity.id)
            except ValueError as exc:
                raise GovernanceInputError(
                    "entity_identity_invalid", "Entity identity must be a UUID."
                ) from exc
            expected_id = UUID(stable_entity_id(command.entity.type, command.entity.name))
            if entity_id != expected_id:
                raise GovernanceInputError(
                    "entity_identity_not_canonical",
                    "A proposed entity ID must derive from its English canonical term.",
                )
            extracted = ExtractedEntity(
                type=command.entity.type,
                name=command.entity.name,
                description=command.entity.description or command.definition,
                evidence_excerpt=command.evidence_excerpt,
                supporting_claims=[card.claim for card in cards],
                confidence=command.extraction_confidence,
            )
            decision = validate_extracted_entity(extracted)
            if not decision.accepted:
                raise GovernanceInputError("entity_validation_failed", decision.reason)
            duplicate = session.scalar(
                select(KnowledgeEntityRecord).where(
                    KnowledgeEntityRecord.canonical_key
                    == canonical_entity_key(command.entity.type, command.entity.name),
                    KnowledgeEntityRecord.status == CanonicalEntityStatus.ACTIVE,
                )
            )
            return {
                "accepted": True,
                "entity_validation": decision.reason,
                "canonical_key": canonical_entity_key(
                    command.entity.type, command.entity.name
                ),
                "duplicate_entity_id": str(duplicate.id) if duplicate else None,
            }

        if not isinstance(command, RelationshipMappingCommand):
            raise GovernanceInputError(
                "mapping_kind_mismatch", "A relationship payload is required."
            )
        source = self._active_entity(session, command.source_entity_id)
        target = self._active_entity(session, command.target_entity_id)
        proposal = RelationshipMappingProposal(
            source_entity_id=str(source.id),
            target_entity_id=str(target.id),
            type=command.relation_type,
            assertion=command.assertion,
            evidence_excerpt=command.evidence_excerpt,
            source_locator=command.source_locator,
            extraction_confidence=command.extraction_confidence,
            supporting_evidence_card_ids=[card.id for card in cards],
        )
        entailment = validate_relationship_entailment(
            proposal,
            self._domain_entity(source),
            self._domain_entity(target),
            cards,
        )
        if not entailment.accepted:
            raise GovernanceInputError(
                "relationship_entailment_failed", entailment.reason
            )
        return {
            "accepted": True,
            "relationship_entailment": entailment.reason,
            "source_entity_id": str(source.id),
            "target_entity_id": str(target.id),
        }

    def _resolve_entity_and_version(
        self,
        session: Session,
        command: EntityMappingCommand,
        review: MappingReviewCommand,
        actor: AuthenticatedActor,
    ) -> tuple[KnowledgeEntityRecord, int, KnowledgeNodeVersionRecord | None]:
        resolution = review.canonical_resolution
        if resolution is None:
            raise GovernanceInputError(
                "canonical_resolution_required",
                "Entity approval requires create, reuse, or merge resolution.",
            )
        proposed_id = UUID(command.entity.id)
        key = canonical_entity_key(command.entity.type, command.entity.name)
        by_key = session.scalar(
            select(KnowledgeEntityRecord)
            .where(KnowledgeEntityRecord.canonical_key == key)
            .with_for_update()
        )
        if resolution == CanonicalResolution.CREATE:
            if review.canonical_entity_id is not None or by_key is not None:
                raise GovernanceConflictError(
                    "canonical_entity_duplicate",
                    "Create is not allowed when the canonical entity already exists.",
                )
            if session.get(KnowledgeEntityRecord, proposed_id) is not None:
                raise GovernanceConflictError(
                    "canonical_entity_id_collision",
                    "The proposed canonical entity ID is already registered.",
                )
            entity = KnowledgeEntityRecord(
                id=proposed_id,
                entity_type=command.entity.type,
                canonical_name=command.entity.name,
                canonical_key=key,
                aliases=_aliases(command.entity.aliases, command.entity.name),
                description=command.entity.description,
                attributes=command.entity.attributes,
                status=CanonicalEntityStatus.ACTIVE,
                created_by_user_id=actor.user_id,
                created_at=self._now(),
                updated_at=self._now(),
            )
            session.add(entity)
            session.flush()
        else:
            target_id = review.canonical_entity_id
            if target_id is None:
                raise GovernanceInputError(
                    "canonical_entity_required",
                    "Reuse or merge requires a canonical entity target.",
                )
            entity = self._active_entity(session, target_id, lock=True)
            if entity.entity_type != command.entity.type:
                raise GovernanceConflictError(
                    "canonical_entity_type_mismatch",
                    "Canonical resolution cannot change entity type.",
                )
            entity.aliases = _aliases(
                [*entity.aliases, command.entity.name, *command.entity.aliases],
                entity.canonical_name,
            )
            entity.updated_at = self._now()
            if resolution == CanonicalResolution.MERGE:
                source = self._active_entity(session, proposed_id, lock=True)
                if source.id == entity.id:
                    raise GovernanceConflictError(
                        "canonical_entity_self_merge",
                        "An entity cannot be merged into itself.",
                    )
                if source.entity_type != entity.entity_type:
                    raise GovernanceConflictError(
                        "canonical_entity_type_mismatch",
                        "Merged entities must have the same entity type.",
                    )
                source.status = CanonicalEntityStatus.MERGED
                source.merged_into_entity_id = entity.id
                source.updated_at = self._now()
            session.flush()

        previous = session.scalar(
            select(KnowledgeNodeVersionRecord)
            .where(KnowledgeNodeVersionRecord.entity_id == entity.id)
            .order_by(KnowledgeNodeVersionRecord.version_number.desc())
            .limit(1)
            .with_for_update()
        )
        return entity, (previous.version_number + 1 if previous else 1), previous

    def _build_node(
        self,
        proposal: MappingProposalRecord,
        command: EntityMappingCommand,
        entity: KnowledgeEntityRecord,
        version: int,
        supersedes: KnowledgeNodeVersionRecord | None,
        cards: list[EvidenceCard],
        confidence: ConfidenceAssessment,
        reviewer: str,
    ) -> KnowledgeNodeVersion:
        canonical = self._domain_entity(entity)
        return KnowledgeNodeVersion(
            entity=canonical,
            version=version,
            definition=command.definition,
            domains=command.domains,
            ontology_paths=command.ontology_paths,
            topic_tags=command.topic_tags,
            knowledge_origin=command.knowledge_origin,
            supporting_evidence_ids=[card.id for card in cards],
            contradicting_evidence_ids=_contradicting_ids(cards),
            limitations=_limitations(command.limitations, cards),
            confidence=confidence,
            review_item_id=str(proposal.id),
            reviewer=reviewer,
            reviewed_at=self._now(),
            supersedes=(
                [str(supersedes.id)] if supersedes is not None else []
            ),
        )

    def _build_relationship_assertion(
        self,
        session: Session,
        proposal: MappingProposalRecord,
        command: RelationshipMappingCommand,
        cards: list[EvidenceCard],
        confidence: ConfidenceAssessment,
        reviewer: str,
        actor: AuthenticatedActor,
    ) -> tuple[
        KnowledgeRelationship,
        KnowledgeRelationshipAssertion,
        KnowledgeRelationshipAssertionRecord | None,
    ]:
        source = self._active_entity(session, command.source_entity_id, lock=True)
        target = self._active_entity(session, command.target_entity_id, lock=True)
        relationship = KnowledgeRelationship(
            source_id=str(source.id),
            target_id=str(target.id),
            type=command.relation_type,
            evidence=None,
            attributes={"reviewed_mapping": True},
        )
        relationship_id = UUID(relationship.id)
        canonical_key = (
            f"{source.id}:{command.relation_type.value}:{target.id}"
        )
        stored = session.scalar(
            select(KnowledgeRelationshipRecord)
            .where(
                KnowledgeRelationshipRecord.source_entity_id == source.id,
                KnowledgeRelationshipRecord.relation_type == command.relation_type,
                KnowledgeRelationshipRecord.target_entity_id == target.id,
            )
            .with_for_update()
        )
        if stored is None:
            stored = KnowledgeRelationshipRecord(
                id=relationship_id,
                source_entity_id=source.id,
                target_entity_id=target.id,
                relation_type=command.relation_type,
                canonical_key=canonical_key,
                attributes=relationship.attributes,
                created_by_user_id=actor.user_id,
                created_at=self._now(),
            )
            session.add(stored)
            session.flush()
        elif stored.id != relationship_id or stored.canonical_key != canonical_key:
            raise GovernanceConflictError(
                "relationship_identity_mismatch",
                "The canonical relationship registry is inconsistent.",
            )
        previous = session.scalar(
            select(KnowledgeRelationshipAssertionRecord)
            .where(
                KnowledgeRelationshipAssertionRecord.relationship_id == stored.id
            )
            .order_by(
                KnowledgeRelationshipAssertionRecord.version_number.desc()
            )
            .limit(1)
            .with_for_update()
        )
        version = previous.version_number + 1 if previous else 1
        assertion = KnowledgeRelationshipAssertion(
            relationship=relationship,
            version=version,
            assertion=command.assertion,
            knowledge_origin=command.knowledge_origin,
            supporting_evidence_ids=[card.id for card in cards],
            contradicting_evidence_ids=_contradicting_ids(cards),
            limitations=_limitations(command.limitations, cards),
            confidence=confidence,
            review_item_id=str(proposal.id),
            reviewer=reviewer,
            reviewed_at=self._now(),
            supersedes=[str(previous.id)] if previous else [],
        )
        return relationship, assertion, previous

    def _validation_run(
        self,
        proposal: MappingProposalRecord,
        actor: AuthenticatedActor,
        evidence_rows: list[EvidenceCardVersion],
        results: dict[str, Any],
    ) -> MappingValidationRun:
        rule_versions = {
            "mapping_review": MAPPING_REVIEW_POLICY_VERSION,
            "entity_validation": ENTITY_VALIDATION_RULE_VERSION,
            "relationship_entailment": RELATIONSHIP_ENTAILMENT_RULE_VERSION,
        }
        input_payload = {
            "proposal_checksum": proposal.proposal_checksum,
            "evidence_versions": [
                {"id": str(row.id), "checksum": row.payload_checksum}
                for row in evidence_rows
            ],
            "rule_versions": rule_versions,
        }
        input_checksum = canonical_payload_hash(input_payload)
        run_payload = {
            "input_checksum": input_checksum,
            "passed": True,
            "results": results,
            "actor_user_id": str(actor.user_id),
        }
        return MappingValidationRun(
            mapping_proposal_id=proposal.id,
            phase=MappingValidationPhase.REVIEW,
            rule_version=self._rule_version(proposal.proposal_kind),
            rule_versions=rule_versions,
            input_checksum=input_checksum,
            passed=True,
            results=results,
            run_checksum=canonical_payload_hash(run_payload),
            actor_user_id=actor.user_id,
            created_at=self._now(),
        )

    def _new_decision(
        self,
        *,
        proposal: MappingProposalRecord,
        sequence: int,
        reviewer: AuthenticatedActor,
        roles: frozenset[RoleName],
        command: MappingReviewCommand,
        status: GovernanceWorkflowStatus,
        validation: MappingValidationRun | None = None,
        artifact_id: UUID | None = None,
        artifact_version: int | None = None,
    ) -> MappingReviewDecision:
        decision_kind = (
            ReviewDecisionKind.APPROVED
            if command.decision == ReviewDecision.APPROVE
            else ReviewDecisionKind.REJECTED
        )
        checksum_payload = {
            "proposal_id": str(proposal.id),
            "sequence": sequence,
            "decision": decision_kind.value,
            "resulting_status": status.value,
            "reviewer_user_id": str(reviewer.user_id),
            "reason": command.reason,
            "confidence": (
                command.confidence.model_dump(mode="json")
                if command.confidence is not None
                else None
            ),
            "canonical_resolution": (
                command.canonical_resolution.value
                if command.canonical_resolution is not None
                else None
            ),
            "canonical_entity_id": (
                str(command.canonical_entity_id)
                if command.canonical_entity_id is not None
                else None
            ),
            "validation_run_id": str(validation.id) if validation else None,
            "approved_artifact_id": str(artifact_id) if artifact_id else None,
            "approved_artifact_version": artifact_version,
            "policy_schema_version": MAPPING_REVIEW_POLICY_VERSION,
        }
        return MappingReviewDecision(
            id=uuid4(),
            mapping_proposal_id=proposal.id,
            sequence_number=sequence,
            decision=decision_kind,
            resulting_status=status,
            reviewer_user_id=reviewer.user_id,
            actor_role_snapshot=[role.value for role in sorted(roles, key=lambda x: x.value)],
            reason=command.reason,
            confidence=(
                command.confidence.model_dump(mode="json")
                if command.confidence is not None
                else None
            ),
            canonical_resolution=(
                command.canonical_resolution.value
                if command.canonical_resolution is not None
                else None
            ),
            canonical_entity_id=command.canonical_entity_id,
            validation_run_id=validation.id if validation else None,
            approved_artifact_id=artifact_id,
            approved_artifact_version=artifact_version,
            decision_checksum=canonical_payload_hash(checksum_payload),
            policy_schema_version=MAPPING_REVIEW_POLICY_VERSION,
            created_at=self._now(),
        )

    def _approve_envelope(
        self,
        session: Session,
        proposal: MappingProposalRecord,
        artifact_id: UUID,
        artifact_version: int,
    ) -> MappingProposalSummary:
        proposal.status = GovernanceWorkflowStatus.APPROVED
        proposal.approved_artifact_id = artifact_id
        proposal.approved_artifact_version = artifact_version
        proposal.updated_at = self._now()
        session.flush()
        return self._summary(proposal)

    def _detail(
        self, session: Session, row: MappingProposalRecord
    ) -> MappingProposalDetail:
        links = list(
            session.scalars(
                select(MappingProposalEvidence)
                .where(MappingProposalEvidence.mapping_proposal_id == row.id)
                .order_by(MappingProposalEvidence.evidence_card_version_id.asc())
            )
        )
        validations = list(
            session.scalars(
                select(MappingValidationRun)
                .where(MappingValidationRun.mapping_proposal_id == row.id)
                .order_by(MappingValidationRun.created_at.asc())
            )
        )
        return MappingProposalDetail(
            **self._summary(row).model_dump(mode="python"),
            proposal=self._parse(row),
            supporting_evidence_version_ids=[
                link.evidence_card_version_id for link in links
            ],
            validation_runs=[
                ValidationRunView(
                    id=value.id,
                    phase=value.phase.value,
                    rule_version=value.rule_version,
                    passed=value.passed,
                    results=value.results,
                    created_at=value.created_at,
                )
                for value in validations
            ],
        )

    @staticmethod
    def _parse(
        row: MappingProposalRecord,
    ) -> EntityMappingCommand | RelationshipMappingCommand:
        try:
            parsed = (
                EntityMappingCommand.model_validate(row.payload)
                if row.proposal_kind == MappingProposalKind.ENTITY
                else RelationshipMappingCommand.model_validate(row.payload)
            )
        except ValidationError as exc:
            raise GovernanceConflictError(
                "mapping_payload_invalid",
                "The persisted Mapping proposal is no longer schema-valid.",
            ) from exc
        if (
            parsed.schema_version != row.schema_version
            or canonical_payload_hash(parsed.model_dump(mode="json"))
            != row.proposal_checksum
        ):
            raise GovernanceConflictError(
                "mapping_checksum_mismatch",
                "The persisted Mapping proposal failed its integrity check.",
            )
        return parsed

    @staticmethod
    def _summary(row: MappingProposalRecord) -> MappingProposalSummary:
        payload = row.payload
        subject = (
            str(payload.get("entity", {}).get("name", "entity"))
            if row.proposal_kind == MappingProposalKind.ENTITY
            else str(payload.get("assertion", "relationship"))
        )
        return MappingProposalSummary(
            id=row.id,
            kind=row.proposal_kind,
            status=WorkflowStatus(row.status.value),
            schema_version=row.schema_version,
            proposal_checksum=row.proposal_checksum,
            subject=subject,
            proposer_user_id=row.proposer_user_id,
            proposer_run_id=row.proposer_run_id,
            sensitivity=row.sensitivity.value,
            submitted_at=row.submitted_at,
            approved_artifact_id=row.approved_artifact_id,
            approved_artifact_version=row.approved_artifact_version,
        )

    @staticmethod
    def _node_view(row: KnowledgeNodeVersionRecord) -> KnowledgeNodeVersionView:
        if canonical_payload_hash(row.payload) != row.payload_checksum:
            raise GovernanceConflictError(
                "knowledge_node_checksum_mismatch",
                "The Knowledge Node version failed its integrity check.",
            )
        try:
            node = KnowledgeNodeVersion.model_validate(row.payload)
        except ValidationError as exc:
            raise GovernanceConflictError(
                "knowledge_node_payload_invalid",
                "The Knowledge Node version is no longer schema-valid.",
            ) from exc
        return KnowledgeNodeVersionView(
            row_id=row.id,
            entity_id=row.entity_id,
            version=row.version_number,
            payload_checksum=row.payload_checksum,
            node=node,
            created_at=row.created_at,
        )

    @staticmethod
    def _assertion_view(
        row: KnowledgeRelationshipAssertionRecord,
    ) -> KnowledgeRelationshipAssertionView:
        if canonical_payload_hash(row.payload) != row.payload_checksum:
            raise GovernanceConflictError(
                "relationship_assertion_checksum_mismatch",
                "The Relationship assertion failed its integrity check.",
            )
        try:
            assertion = KnowledgeRelationshipAssertion.model_validate(row.payload)
            domains = [MagicDomain(value) for value in row.projection_context["domains"]]
            ontology_paths = list(row.projection_context["ontology_paths"])
            topic_tags = list(row.projection_context.get("topic_tags", []))
        except (ValidationError, KeyError, TypeError, ValueError) as exc:
            raise GovernanceConflictError(
                "relationship_assertion_payload_invalid",
                "The Relationship assertion is no longer schema-valid.",
            ) from exc
        return KnowledgeRelationshipAssertionView(
            row_id=row.id,
            relationship_id=row.relationship_id,
            version=row.version_number,
            payload_checksum=row.payload_checksum,
            assertion=assertion,
            domains=domains,
            ontology_paths=ontology_paths,
            topic_tags=topic_tags,
            created_at=row.created_at,
        )

    @staticmethod
    def _domain_entity(row: KnowledgeEntityRecord) -> KnowledgeEntity:
        return KnowledgeEntity(
            id=str(row.id),
            type=row.entity_type,
            name=row.canonical_name,
            description=row.description,
            aliases=row.aliases,
            attributes=row.attributes,
        )

    @staticmethod
    def _active_entity(
        session: Session, entity_id: UUID, *, lock: bool = False
    ) -> KnowledgeEntityRecord:
        statement = select(KnowledgeEntityRecord).where(
            KnowledgeEntityRecord.id == entity_id,
            KnowledgeEntityRecord.status == CanonicalEntityStatus.ACTIVE,
        )
        if lock:
            statement = statement.with_for_update()
        row = session.scalar(statement)
        if row is None:
            raise GovernanceConflictError(
                "canonical_entity_not_active",
                "A relationship or resolution endpoint is not an active canonical entity.",
            )
        return row

    @staticmethod
    def _rule_version(kind: MappingProposalKind) -> str:
        semantic = (
            ENTITY_VALIDATION_RULE_VERSION
            if kind == MappingProposalKind.ENTITY
            else RELATIONSHIP_ENTAILMENT_RULE_VERSION
        )
        return f"{MAPPING_REVIEW_POLICY_VERSION}+{semantic}"

    @staticmethod
    def _validate_rejection_shape(command: MappingReviewCommand) -> None:
        if (
            command.confidence is not None
            or command.canonical_resolution is not None
            or command.canonical_entity_id is not None
        ):
            raise GovernanceInputError(
                "mapping_rejection_shape_invalid",
                "A Mapping rejection cannot resolve an entity or assess confidence.",
            )

    def _review_confidence(
        self, command: MappingReviewCommand, reviewer: str
    ) -> ConfidenceAssessment:
        if command.confidence is None:
            raise GovernanceInputError(
                "mapping_confidence_required",
                "A Mapping approval requires confidence.",
            )
        if command.confidence.assessed_by.strip() != reviewer:
            raise GovernanceInputError(
                "mapping_confidence_reviewer_mismatch",
                "Mapping confidence must be assessed by the authenticated reviewer.",
            )
        if command.confidence.label == ConfidenceLabel.INSUFFICIENT:
            raise GovernanceInputError(
                "mapping_confidence_insufficient",
                "Insufficient confidence cannot approve a Mapping.",
            )
        return ConfidenceAssessment.model_validate(
            {
                **command.confidence.model_dump(mode="python"),
                "assessed_by": reviewer,
                "assessed_at": self._now(),
            }
        )

    @staticmethod
    def _human(username: str) -> str:
        try:
            return require_human_identity(username)
        except ValueError as exc:
            raise GovernanceAuthorizationError(
                "human_reviewer_required", "A named human reviewer is required."
            ) from exc

    @staticmethod
    def _next_decision_sequence(session: Session, proposal_id: UUID) -> int:
        current = session.scalar(
            select(func.max(MappingReviewDecision.sequence_number)).where(
                MappingReviewDecision.mapping_proposal_id == proposal_id
            )
        )
        return int(current or 0) + 1

    def _require_permission(
        self,
        session: Session,
        actor: AuthenticatedActor,
        permission: Permission,
    ) -> tuple[AuthenticatedActor, frozenset[RoleName]]:
        if actor.user_id is None or actor.session_id is None or actor.is_bootstrap_anonymous:
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
        if not has_permission(current, permission):
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
        try:
            record, replay = IdempotencyRepository(session).begin(
                actor_user_id=actor.user_id,
                scope=scope,
                key=key,
                request_hash=canonical_payload_hash(payload),
                now=self._now(),
            )
        except IdempotencyConflictError as exc:
            raise GovernanceConflictError("idempotency_conflict", str(exc)) from exc
        except IdempotencyInProgressError as exc:
            raise GovernanceConflictError("idempotency_in_progress", str(exc)) from exc
        except IdempotencyError as exc:
            raise GovernanceInputError("invalid_idempotency_key", str(exc)) from exc
        return record, replay.body if replay is not None else None

    def _complete_idempotency(
        self,
        session: Session,
        record,
        response: MappingProposalSummary | MappingReviewResult,
        status_code: int,
    ) -> None:
        IdempotencyRepository(session).complete(
            record,
            status_code=status_code,
            response_body=response.model_dump(mode="json"),
            now=self._now(),
        )

    @staticmethod
    def _latest_mapping_decision(
        session: Session, proposal_id: UUID
    ) -> MappingReviewDecision | None:
        return session.scalar(
            select(MappingReviewDecision)
            .where(MappingReviewDecision.mapping_proposal_id == proposal_id)
            .order_by(MappingReviewDecision.sequence_number.desc())
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

    def _effective_source_sensitivity(
        self,
        session: Session,
        source_version_id: UUID,
    ) -> SensitiveInformationLevel:
        version = session.get(SourceVersion, source_version_id)
        if version is None:
            raise GovernanceConflictError(
                "source_version_missing",
                "A supporting Evidence Source version is unavailable.",
            )
        levels = [version.sensitivity]
        source_decision = session.scalar(
            select(SourceReviewDecision)
            .where(SourceReviewDecision.source_version_id == source_version_id)
            .order_by(SourceReviewDecision.sequence_number.desc())
            .limit(1)
        )
        if source_decision is not None:
            levels.append(source_decision.sensitive_information_level)
        candidates = list(
            session.scalars(
                select(ClaimCandidate).where(
                    ClaimCandidate.source_version_id == source_version_id
                )
            )
        )
        for candidate in candidates:
            claim_decision = self._latest_claim_decision(session, candidate.id)
            if claim_decision is not None:
                levels.append(claim_decision.sensitive_information_level)
            evidence_row = session.scalar(
                select(EvidenceCardVersion)
                .where(EvidenceCardVersion.claim_candidate_id == candidate.id)
                .order_by(
                    EvidenceCardVersion.version_number.desc(),
                    EvidenceCardVersion.created_at.desc(),
                    EvidenceCardVersion.id.desc(),
                )
                .limit(1)
            )
            if evidence_row is None:
                continue
            if canonical_payload_hash(evidence_row.payload) != evidence_row.payload_checksum:
                levels.append(SensitiveInformationLevel.RESTRICTED)
                continue
            try:
                evidence = EvidenceCard.model_validate(evidence_row.payload)
            except ValidationError:
                levels.append(SensitiveInformationLevel.RESTRICTED)
            else:
                levels.append(evidence.review.sensitive_information_level)
        return max(levels, key=_sensitivity_rank)

    @staticmethod
    def _validate_current_source_policy(
        card: EvidenceCard,
        claim_decision: ClaimReviewDecision,
        source_decision: SourceReviewDecision,
    ) -> None:
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
            key=_storage_permission_rank,
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
        ):
            raise GovernanceConflictError(
                "evidence_governance_mismatch",
                "Evidence Card governance no longer matches current Source approval.",
            )

    @staticmethod
    def _ensure_sensitivity_visible(
        actor: AuthenticatedActor, sensitivity: SensitiveInformationLevel
    ) -> None:
        authorization = retrieval_authorization_for_actor(actor)
        if sensitivity not in authorization.allowed_sensitive_levels:
            raise GovernanceAuthorizationError()

    @staticmethod
    def _maximum_sensitivity(cards: list[EvidenceCard]) -> SensitiveInformationLevel:
        rank = {
            SensitiveInformationLevel.PUBLIC: 0,
            SensitiveInformationLevel.CONTROLLED: 1,
            SensitiveInformationLevel.SECRET_METHOD: 2,
            SensitiveInformationLevel.RESTRICTED: 3,
        }
        return max(
            (card.review.sensitive_information_level for card in cards),
            key=rank.__getitem__,
        )

    def _now(self) -> datetime:
        return _aware_utc(self._clock())


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _claim_eligibility_rank(value: ClaimEligibility) -> int:
    return {
        ClaimEligibility.INELIGIBLE: 0,
        ClaimEligibility.NOT_ASSESSED: 1,
        ClaimEligibility.ELIGIBLE_WITH_LIMITS: 2,
        ClaimEligibility.ELIGIBLE: 3,
    }[value]


def _storage_permission_rank(value: StoragePermission) -> int:
    return {
        StoragePermission.NONE: 0,
        StoragePermission.DERIVED_KNOWLEDGE_ONLY: 1,
        StoragePermission.DERIVED_WITH_SHORT_EXCERPT: 2,
    }[value]


def _sensitivity_rank(value: SensitiveInformationLevel) -> int:
    return {
        SensitiveInformationLevel.PUBLIC: 0,
        SensitiveInformationLevel.CONTROLLED: 1,
        SensitiveInformationLevel.SECRET_METHOD: 2,
        SensitiveInformationLevel.RESTRICTED: 3,
    }[value]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _aliases(values: Sequence[str], canonical_name: str) -> list[str]:
    canonical = canonical_name.casefold()
    normalized = [" ".join(value.split()) for value in values if value.strip()]
    return list(dict.fromkeys(value for value in normalized if value.casefold() != canonical))


def _contradicting_ids(cards: Sequence[EvidenceCard]) -> list[str]:
    return list(
        dict.fromkeys(
            evidence_id
            for card in cards
            for evidence_id in card.contradicting_evidence_ids
        )
    )


def _limitations(values: Sequence[str], cards: Sequence[EvidenceCard]) -> list[str]:
    return list(
        dict.fromkeys(
            " ".join(value.split())
            for value in [*values, *(item for card in cards for item in card.limitations)]
            if value.strip()
        )
    )


__all__ = ["ProductionMappingService"]
