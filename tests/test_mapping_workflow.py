"""Production Mapping governance service integration tests."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from knowledge.evidence import KnowledgeOrigin, MagicDomain
from knowledge.governance import StoragePermission
from knowledge.models import EntityType, KnowledgeEntity, RelationType
from persistence import (
    AppendOnlyMutationError,
    AuditEvent,
    EvidenceCardVersion,
    GovernanceWorkflowStatus,
    KnowledgeEntityRecord,
    KnowledgeNodeVersionRecord,
    KnowledgeRelationshipAssertionRecord,
    KnowledgeRelationshipRecord,
    MappingProposalRecord,
    MappingReviewDecision,
    MappingValidationRun,
    ReviewDecisionKind,
    SourceReviewDecision,
)
from research.review.mapping_models import (
    CanonicalResolution,
    EntityMappingCommand,
    MappingReviewCommand,
    RelationshipMappingCommand,
)
from research.review.mapping_service import ProductionMappingService
from research.review.workflow_models import ReviewDecision, WorkflowStatus
from research.review.workflow_service import (
    GovernanceAuthorizationError,
    GovernanceConflictError,
)
from test_governance_workflow import (
    CLAIM_TEXT,
    NOW,
    _claim_review_command,
    _confidence,
    _register_and_approve_source,
    _submit_claim,
    governance,
)


LOCATOR = "https://example.test/papers/claim-locator"


@pytest.fixture
def mapped(governance):
    _source_id, source_version_id = _register_and_approve_source(
        governance, slug="mapping-foundation"
    )
    claim = _submit_claim(
        governance,
        source_version_id,
        key="mapping-claim",
    )
    approved = governance.service.review_claim(
        governance.reviewer,
        claim.id,
        _claim_review_command(governance.reviewer.username),
        idempotency_key="mapping-claim-review",
    )
    assert approved.evidence_version_id is not None
    return governance, ProductionMappingService(
        governance.database.session_factory, clock=lambda: NOW
    ), approved.evidence_version_id


def _entity_command(
    evidence_version_id,
    *,
    entity_type: EntityType = EntityType.PSYCHOLOGY_PRINCIPLE,
    name: str = "Inattentional Blindness",
    definition: str = "An applied attention principle concerning unperceived events.",
) -> EntityMappingCommand:
    return EntityMappingCommand(
        entity=KnowledgeEntity(type=entity_type, name=name),
        definition=definition,
        domains=[MagicDomain.THEORY],
        ontology_paths=["psychology.attention.inattentional_blindness"],
        topic_tags=["attention", "misdirection"],
        knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        evidence_excerpt=CLAIM_TEXT,
        source_locator=LOCATOR,
        extraction_confidence=0.9,
        supporting_evidence_version_ids=[evidence_version_id],
        limitations=["The finding comes from one controlled context."],
        proposer_run_id="mapping-test-run",
    )


def _approve(reviewer, *, resolution=CanonicalResolution.CREATE, target=None):
    return MappingReviewCommand(
        decision=ReviewDecision.APPROVE,
        reason="The entity and its exact evidence version were independently checked.",
        confidence=_confidence(reviewer.username),
        canonical_resolution=resolution,
        canonical_entity_id=target,
    )


def _count(session, model) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _reduce_current_source_storage(governance, evidence_version_id) -> None:
    with governance.database.session_factory() as session:
        evidence = session.get(EvidenceCardVersion, evidence_version_id)
        assert evidence is not None
        latest = session.scalar(
            select(SourceReviewDecision)
            .where(
                SourceReviewDecision.source_version_id
                == evidence.source_version_id
            )
            .order_by(SourceReviewDecision.sequence_number.desc())
            .limit(1)
        )
        assert latest is not None
        session.add(
            SourceReviewDecision(
                source_version_id=evidence.source_version_id,
                source_permission_request_id=(
                    latest.source_permission_request_id
                ),
                sequence_number=latest.sequence_number + 1,
                decision=ReviewDecisionKind.CORRECTED,
                resulting_status=GovernanceWorkflowStatus.APPROVED,
                reviewer_user_id=latest.reviewer_user_id,
                actor_role_snapshot=list(latest.actor_role_snapshot),
                reason="Reduce current Source storage authority after approval.",
                citation_verification_evidence_id=(
                    latest.citation_verification_evidence_id
                ),
                claim_eligibility=latest.claim_eligibility,
                extraction_permission=latest.extraction_permission,
                storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
                sensitive_information_level=latest.sensitive_information_level,
                contradicting_evidence_checked=(
                    latest.contradicting_evidence_checked
                ),
                extraction_scope=dict(latest.extraction_scope),
                domain_schema_version=latest.domain_schema_version,
                decision_checksum="c" * 64,
                supersedes_decision_id=latest.id,
                created_at=NOW + timedelta(minutes=1),
            )
        )
        session.commit()


def test_entity_mapping_is_versioned_audited_and_idempotent(mapped) -> None:
    governance, service, evidence_version_id = mapped
    command = _entity_command(evidence_version_id)
    submitted = service.submit_entity(
        governance.operator,
        command,
        idempotency_key="mapping-entity-submit",
    )
    replay = service.submit_entity(
        governance.operator,
        command,
        idempotency_key="mapping-entity-submit",
    )
    assert replay == submitted
    assert submitted.status == WorkflowStatus.SUBMITTED

    approved = service.review_mapping(
        governance.reviewer,
        submitted.id,
        _approve(governance.reviewer),
        idempotency_key="mapping-entity-review",
    )
    review_replay = service.review_mapping(
        governance.reviewer,
        submitted.id,
        _approve(governance.reviewer),
        idempotency_key="mapping-entity-review",
    )
    assert review_replay == approved
    assert approved.mapping.status == WorkflowStatus.APPROVED
    assert approved.knowledge_node_version_id is not None

    node = service.get_node_version(
        governance.reviewer, approved.knowledge_node_version_id
    )
    assert node.version == 1
    assert node.node.entity.name == "Inattentional Blindness"
    assert node.node.supporting_evidence_ids
    with governance.database.session_factory() as session:
        assert _count(session, MappingProposalRecord) == 1
        assert _count(session, MappingReviewDecision) == 1
        assert _count(session, MappingValidationRun) == 1
        assert _count(session, KnowledgeEntityRecord) == 1
        assert _count(session, KnowledgeNodeVersionRecord) == 1
        assert _count(session, AuditEvent) >= 2


def test_node_detail_and_registry_recheck_current_evidence_policy(mapped) -> None:
    governance, service, evidence_version_id = mapped
    submitted = service.submit_entity(
        governance.operator,
        _entity_command(evidence_version_id),
        idempotency_key="mapping-current-detail-submit",
    )
    approved = service.review_mapping(
        governance.reviewer,
        submitted.id,
        _approve(governance.reviewer),
        idempotency_key="mapping-current-detail-review",
    )
    assert approved.knowledge_node_version_id is not None
    assert service.get_node_version(
        governance.reviewer, approved.knowledge_node_version_id
    )

    _reduce_current_source_storage(governance, evidence_version_id)

    with pytest.raises(GovernanceConflictError) as stale:
        service.get_node_version(
            governance.reviewer, approved.knowledge_node_version_id
        )
    assert stale.value.code == "evidence_governance_mismatch"
    assert service.list_canonical_entities(governance.reviewer) == []


def test_mapping_rbac_self_review_and_current_evidence_are_enforced(mapped) -> None:
    governance, service, evidence_version_id = mapped
    command = _entity_command(evidence_version_id)
    with pytest.raises(GovernanceAuthorizationError):
        service.submit_entity(
            governance.reader,
            command,
            idempotency_key="mapping-reader-submit",
        )
    with pytest.raises(GovernanceAuthorizationError):
        service.submit_entity(
            governance.reviewer,
            command,
            idempotency_key="mapping-reviewer-submit",
        )

    own = service.submit_entity(
        governance.dual_role,
        command,
        idempotency_key="mapping-dual-submit",
    )
    with pytest.raises(GovernanceAuthorizationError) as self_review:
        service.review_mapping(
            governance.dual_role,
            own.id,
            _approve(governance.dual_role),
            idempotency_key="mapping-dual-review",
        )
    assert self_review.value.code == "mapping_self_review_forbidden"

    submitted = service.submit_entity(
        governance.operator,
        _entity_command(
            evidence_version_id,
            definition="A second proposal whose evidence may later be revoked.",
        ),
        idempotency_key="mapping-revocation-submit",
    )
    with governance.database.session_factory() as session:
        evidence = session.get(EvidenceCardVersion, evidence_version_id)
        from persistence import ClaimCandidate, ClaimReviewDecision, ReviewDecisionKind
        from knowledge.evidence import ContradictionStatus
        from knowledge.governance import (
            ClaimEligibility,
            ContradictionCheckStatus,
            SecretExposureLevel,
            SensitiveInformationLevel,
            StoragePermission,
        )
        claim = session.get(ClaimCandidate, evidence.claim_candidate_id)
        latest = session.scalar(
            select(ClaimReviewDecision)
            .where(ClaimReviewDecision.claim_candidate_id == claim.id)
            .order_by(ClaimReviewDecision.sequence_number.desc())
            .limit(1)
        )
        terminal = ClaimReviewDecision(
            claim_candidate_id=claim.id,
            sequence_number=latest.sequence_number + 1,
            decision=ReviewDecisionKind.REVOKED,
            resulting_status=GovernanceWorkflowStatus.REVOKED,
            reviewer_user_id=governance.reviewer.user_id,
            actor_role_snapshot=["reviewer"],
            reason="Revoke the claim to test Mapping revalidation.",
            confidence=None,
            limitations=[],
            evidence_class=None,
            knowledge_origin=None,
            evidence_level=None,
            claim_eligibility=ClaimEligibility.INELIGIBLE,
            storage_permission=StoragePermission.NONE,
            contradiction_status=ContradictionStatus.NOT_CHECKED,
            contradiction_check_status=ContradictionCheckStatus.NOT_CHECKED,
            contradicting_evidence_ids=[],
            sensitive_information_level=SensitiveInformationLevel.CONTROLLED,
            secret_exposure_level=SecretExposureLevel.GENERAL_PRINCIPLE,
            policy_schema_version="claim-review-decision-0.1",
            decision_checksum="a" * 64,
            supersedes_decision_id=latest.id,
            created_at=NOW + timedelta(minutes=1),
        )
        session.add(terminal)
        session.flush()
        claim.status = GovernanceWorkflowStatus.REVOKED
        session.commit()

    with pytest.raises(GovernanceConflictError) as revoked:
        service.review_mapping(
            governance.reviewer,
            submitted.id,
            _approve(governance.reviewer),
            idempotency_key="mapping-revoked-review",
        )
    assert revoked.value.code == "evidence_not_currently_eligible"


def test_duplicate_requires_explicit_reuse_and_creates_node_version(mapped) -> None:
    governance, service, evidence_version_id = mapped
    first = service.submit_entity(
        governance.operator,
        _entity_command(evidence_version_id),
        idempotency_key="mapping-first-submit",
    )
    first_review = service.review_mapping(
        governance.reviewer,
        first.id,
        _approve(governance.reviewer),
        idempotency_key="mapping-first-review",
    )
    with governance.database.session_factory() as session:
        first_node = session.get(
            KnowledgeNodeVersionRecord, first_review.knowledge_node_version_id
        )
        entity_id = first_node.entity_id

    second = service.submit_entity(
        governance.operator,
        _entity_command(
            evidence_version_id,
            definition="A refined definition that preserves the same canonical identity.",
        ),
        idempotency_key="mapping-second-submit",
    )
    with pytest.raises(GovernanceConflictError) as duplicate:
        service.review_mapping(
            governance.reviewer,
            second.id,
            _approve(governance.reviewer),
            idempotency_key="mapping-second-create-review",
        )
    assert duplicate.value.code == "canonical_entity_duplicate"

    approved = service.review_mapping(
        governance.reviewer,
        second.id,
        _approve(
            governance.reviewer,
            resolution=CanonicalResolution.REUSE,
            target=entity_id,
        ),
        idempotency_key="mapping-second-reuse-review",
    )
    node = service.get_node_version(
        governance.reviewer, approved.knowledge_node_version_id
    )
    assert node.version == 2
    assert len(node.node.supersedes) == 1


def test_relationship_identity_excludes_assertion_evidence(mapped) -> None:
    governance, service, evidence_version_id = mapped
    entity_ids = []
    for suffix, entity_type, name, definition in (
        (
            "principle",
            EntityType.PSYCHOLOGY_PRINCIPLE,
            "Inattentional Blindness",
            "An applied attention principle concerning unperceived events.",
        ),
        (
            "effect",
            EntityType.EFFECT,
            "Failure to Notice an Unexpected Action",
            "An audience experiences an unexpected action without perceiving it.",
        ),
    ):
        proposal = service.submit_entity(
            governance.operator,
            _entity_command(
                evidence_version_id,
                entity_type=entity_type,
                name=name,
                definition=definition,
            ),
            idempotency_key=f"relationship-{suffix}-submit",
        )
        result = service.review_mapping(
            governance.reviewer,
            proposal.id,
            _approve(governance.reviewer),
            idempotency_key=f"relationship-{suffix}-review",
        )
        with governance.database.session_factory() as session:
            node = session.get(
                KnowledgeNodeVersionRecord, result.knowledge_node_version_id
            )
            entity_ids.append(node.entity_id)

    command = RelationshipMappingCommand(
        source_entity_id=entity_ids[0],
        target_entity_id=entity_ids[1],
        relation_type=RelationType.EXPLAINS,
        assertion=(
            "Inattentional Blindness explains the failure to notice an "
            "unexpected action."
        ),
        domains=[MagicDomain.THEORY],
        ontology_paths=["psychology.attention.inattentional_blindness"],
        topic_tags=["attention"],
        knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        evidence_excerpt=CLAIM_TEXT,
        source_locator=LOCATOR,
        extraction_confidence=0.9,
        supporting_evidence_version_ids=[evidence_version_id],
        limitations=["The explanatory edge is limited to the reviewed context."],
    )
    proposal = service.submit_relationship(
        governance.operator,
        command,
        idempotency_key="relationship-submit",
    )
    review = MappingReviewCommand(
        decision=ReviewDecision.APPROVE,
        reason="The explanatory assertion and endpoints were checked.",
        confidence=_confidence(governance.reviewer.username),
    )
    approved = service.review_mapping(
        governance.reviewer,
        proposal.id,
        review,
        idempotency_key="relationship-review",
    )
    view = service.get_relationship_assertion(
        governance.reviewer, approved.relationship_assertion_id
    )
    assert view.assertion.relationship.evidence is None
    with governance.database.session_factory() as session:
        assert _count(session, KnowledgeRelationshipRecord) == 1
        assert _count(session, KnowledgeRelationshipAssertionRecord) == 1
        relationship = session.scalar(select(KnowledgeRelationshipRecord))
        assert relationship.canonical_key == (
            f"{entity_ids[0]}:explains:{entity_ids[1]}"
        )

    _reduce_current_source_storage(governance, evidence_version_id)
    with pytest.raises(GovernanceConflictError) as stale:
        service.get_relationship_assertion(
            governance.reviewer, approved.relationship_assertion_id
        )
    assert stale.value.code == "evidence_governance_mismatch"


def test_mapping_history_rows_and_payload_are_append_only(mapped) -> None:
    governance, service, evidence_version_id = mapped
    submitted = service.submit_entity(
        governance.operator,
        _entity_command(evidence_version_id),
        idempotency_key="append-only-mapping-submit",
    )
    approved = service.review_mapping(
        governance.reviewer,
        submitted.id,
        _approve(governance.reviewer),
        idempotency_key="append-only-mapping-review",
    )
    with governance.database.session_factory() as session:
        proposal = session.get(MappingProposalRecord, submitted.id)
        proposal.payload = {**proposal.payload, "definition": "tampered"}
        with pytest.raises(AppendOnlyMutationError):
            session.flush()
        session.rollback()

    with governance.database.session_factory() as session:
        node = session.get(
            KnowledgeNodeVersionRecord, approved.knowledge_node_version_id
        )
        node.payload = {**node.payload, "definition": "tampered"}
        with pytest.raises(AppendOnlyMutationError):
            session.flush()
