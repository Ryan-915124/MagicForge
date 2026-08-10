"""Fail-closed tests for current approvals at storage and read boundaries."""

from __future__ import annotations

from datetime import timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import select, update

from app.knowledge_read_model import KnowledgeReadModelError
from app.production_read_model import (
    ProductionKnowledgeReadModel,
    _LoadedArtifact,
    _safe_search_projection,
)
from knowledge.evidence import EvidenceCard
from knowledge.governance import ExtractionPermission, StoragePermission
from knowledge.projections import (
    KnowledgeNodeVersion,
    ProjectionBuilder,
    StorageManifest,
)
from persistence import (
    CanonicalEntityStatus,
    EvidenceCardVersion,
    GovernanceWorkflowStatus,
    KnowledgeEntityRecord,
    KnowledgeNodeVersionRecord,
    MappingProposalEvidence,
    ReviewDecisionKind,
    RoleName,
    SourceReviewDecision,
    SourceVersion,
)
from persistence.models import SourcePermissionRequest
from persistence.idempotency import canonical_payload_hash
from persistence.storage_models import (
    IngestionOperationRecord,
    IngestionReceiptRecord,
    StorageManifestRecord,
    StorageManifestState,
)
from research.review.mapping_service import ProductionMappingService
from research.review.source_permission_policy import (
    SourcePermissionPolicyError,
    permission_request_checksum_payload,
    require_current_source_approval,
)
from research.review.storage_workflow_models import (
    ManifestAuthorizationCommand,
    ManifestBuildCommand,
    ManifestIngestionCommand,
)
from research.review.storage_workflow_service import (
    ProductionStorageWorkflowService,
    StorageConflictError,
)
from research.review.workflow_service import (
    GovernanceConflictError,
    GovernanceNotFoundError,
)
import research.review.storage_workflow_service as storage_workflow_module
from test_governance_workflow import (
    CLAIM_TEXT,
    NOW,
    _claim_command,
    _claim_review_command,
    _register_and_approve_source,
    _source_command,
    governance,
)
from test_mapping_workflow import _approve, _entity_command
from test_storage_workflow import FakeWriter


def _approved_evidence(
    governance,
    *,
    slug: str,
    storage_permission: StoragePermission = StoragePermission.DERIVED_WITH_SHORT_EXCERPT,
):
    _source_id, source_version_id = _register_and_approve_source(
        governance,
        slug=slug,
        storage_permission=storage_permission,
    )
    candidate = governance.service.submit_claim(
        governance.operator,
        _claim_command(
            source_version_id,
            claim="Participants may overlook an unexpected action in a demonstration.",
        ).model_copy(update={"evidence_excerpt": CLAIM_TEXT}),
        idempotency_key=f"{slug}-claim",
    )
    approval = governance.service.review_claim(
        governance.reviewer,
        candidate.id,
        _claim_review_command(
            governance.reviewer.username,
            storage_permission=storage_permission,
        ),
        idempotency_key=f"{slug}-claim-review",
    )
    assert approval.evidence_version_id is not None
    return source_version_id, approval.evidence_version_id


def _storage(governance) -> ProductionStorageWorkflowService:
    return ProductionStorageWorkflowService(
        governance.database.session_factory,
        vector_size=3,
        vector_distance="cosine",
        clock=lambda: NOW,
    )


def _build_command(*, evidence_id, node_id=None) -> ManifestBuildCommand:
    return ManifestBuildCommand(
        corpus_id=uuid4(),
        collection_name=f"magicforge_cp4_test_{uuid4().hex}",
        evidence_version_ids=[evidence_id],
        knowledge_node_version_ids=[node_id] if node_id is not None else [],
    )


def _append_incompatible_current_source_decision(governance, source_version_id) -> None:
    """Simulate a later approved Source correction without mutating history."""

    with governance.database.session_factory() as session:
        previous = session.scalar(
            select(SourceReviewDecision)
            .where(SourceReviewDecision.source_version_id == source_version_id)
            .order_by(SourceReviewDecision.sequence_number.desc())
            .limit(1)
        )
        assert previous is not None
        checksum = canonical_payload_hash(
            {
                "source_version_id": str(source_version_id),
                "sequence": previous.sequence_number + 1,
                "correction": "storage permission reduced",
            }
        )
        session.add(
            SourceReviewDecision(
                id=uuid4(),
                source_version_id=source_version_id,
                source_permission_request_id=(
                    previous.source_permission_request_id
                ),
                sequence_number=previous.sequence_number + 1,
                decision=ReviewDecisionKind.CORRECTED,
                resulting_status=GovernanceWorkflowStatus.APPROVED,
                reviewer_user_id=previous.reviewer_user_id,
                actor_role_snapshot=list(previous.actor_role_snapshot),
                reason="Reduce current Source storage authority after review.",
                citation_verification_evidence_id=(
                    previous.citation_verification_evidence_id
                ),
                claim_eligibility=previous.claim_eligibility,
                extraction_permission=previous.extraction_permission,
                storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
                sensitive_information_level=previous.sensitive_information_level,
                contradicting_evidence_checked=(
                    previous.contradicting_evidence_checked
                ),
                extraction_scope=dict(previous.extraction_scope),
                domain_schema_version=previous.domain_schema_version,
                decision_checksum=checksum,
                supersedes_decision_id=previous.id,
                created_at=NOW + timedelta(seconds=1),
            )
        )
        session.commit()


def _append_valid_permission_request_after_approval(
    governance,
    source_version_id,
) -> None:
    """Simulate a valid later rights amendment inserted by a migration/import."""

    with governance.database.session_factory() as session:
        previous = session.scalar(
            select(SourcePermissionRequest)
            .where(SourcePermissionRequest.source_version_id == source_version_id)
            .order_by(SourcePermissionRequest.sequence_number.desc())
            .limit(1)
        )
        assert previous is not None
        sequence = previous.sequence_number + 1
        rights_evidence = [
            *previous.rights_evidence,
            f"migration-rights-amendment:{sequence}",
        ]
        reason = "Append a later valid rights request to invalidate the old decision."
        roles = [RoleName.OPERATOR.value]
        payload = permission_request_checksum_payload(
            source_version_id=source_version_id,
            sequence_number=sequence,
            requested_extraction_permission=previous.requested_extraction_permission,
            requested_storage_permission=previous.requested_storage_permission,
            requested_scope_locators=list(previous.requested_scope_locators),
            rights_basis=previous.rights_basis,
            rights_evidence=rights_evidence,
            reason=reason,
            submitted_by_user_id=governance.operator.user_id,
            actor_role_snapshot=roles,
            supersedes_request_id=previous.id,
        )
        checksum = canonical_payload_hash(payload)
        session.add(
            SourcePermissionRequest(
                id=uuid5(
                    NAMESPACE_URL,
                    "magicforge:source-permission-request:"
                    f"{source_version_id}:{sequence}:{checksum}",
                ),
                source_version_id=source_version_id,
                sequence_number=sequence,
                requested_extraction_permission=(
                    previous.requested_extraction_permission
                ),
                requested_storage_permission=previous.requested_storage_permission,
                requested_scope_locators=list(previous.requested_scope_locators),
                rights_basis=previous.rights_basis,
                rights_evidence=rights_evidence,
                reason=reason,
                submitted_by_user_id=governance.operator.user_id,
                actor_role_snapshot=roles,
                domain_schema_version="source-permission-request-0.1",
                request_checksum=checksum,
                supersedes_request_id=previous.id,
                created_at=NOW + timedelta(seconds=2),
            )
        )
        session.commit()


class _UnexpectedWriter:
    """A write probe: approval reconciliation must fail before this boundary."""

    def __init__(self) -> None:
        self.write_calls = 0

    def write_manifest(self, _manifest, *, actor):
        del actor
        self.write_calls += 1
        raise AssertionError("current-approval preflight must prevent vector writes")

    def cleanup_manifest_points(self, _manifest):
        raise AssertionError("no vector cleanup is needed before a write starts")


def test_current_approval_rejects_missing_and_cross_version_request_bindings(
    governance,
) -> None:
    _source_id, source_version_id = _register_and_approve_source(
        governance,
        slug="permission-binding-policy-source",
        storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
    )
    other = governance.service.register_source(
        governance.operator,
        _source_command(
            slug="permission-binding-policy-other",
            storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
        ),
        idempotency_key="permission-binding-policy-other-register",
    )
    other_request_id = other.versions[0].latest_permission_request_id
    assert other_request_id is not None

    with governance.database.session_factory() as session:
        version = session.get(SourceVersion, source_version_id)
        decision = session.scalar(
            select(SourceReviewDecision).where(
                SourceReviewDecision.source_version_id == source_version_id
            )
        )
        assert version is not None and decision is not None
        session.expunge(decision)
        decision.source_permission_request_id = uuid4()
        with pytest.raises(SourcePermissionPolicyError) as missing:
            require_current_source_approval(session, version, decision)
        assert missing.value.code == "source_permission_request_binding_invalid"

    with governance.database.session_factory() as session:
        version = session.get(SourceVersion, source_version_id)
        decision = session.scalar(
            select(SourceReviewDecision).where(
                SourceReviewDecision.source_version_id == source_version_id
            )
        )
        assert version is not None and decision is not None
        session.expunge(decision)
        decision.source_permission_request_id = other_request_id
        with pytest.raises(SourcePermissionPolicyError) as cross_version:
            require_current_source_approval(session, version, decision)
        assert (
            cross_version.value.code
            == "source_permission_request_binding_invalid"
        )


def test_later_permission_request_invalidates_claim_mapping_storage_and_reads(
    governance,
) -> None:
    source_version_id, evidence_id = _approved_evidence(
        governance,
        slug="permission-request-stale-downstream",
        storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
    )
    _append_valid_permission_request_after_approval(
        governance,
        source_version_id,
    )

    with pytest.raises(GovernanceConflictError) as claim_blocked:
        governance.service.submit_claim(
            governance.operator,
            _claim_command(
                source_version_id,
                claim="A later permission request invalidates the old extraction decision.",
            ),
            idempotency_key="permission-request-stale-claim",
        )
    assert claim_blocked.value.code == "source_permission_request_stale"

    mapping = ProductionMappingService(
        governance.database.session_factory,
        clock=lambda: NOW,
    )
    with pytest.raises(GovernanceConflictError) as mapping_blocked:
        mapping.submit_entity(
            governance.operator,
            _entity_command(evidence_id),
            idempotency_key="permission-request-stale-mapping",
        )
    assert mapping_blocked.value.code == "source_permission_request_stale"

    storage = _storage(governance)
    with pytest.raises(StorageConflictError) as storage_blocked:
        storage.build_manifest(
            governance.operator,
            _build_command(evidence_id=evidence_id),
            idempotency_key="permission-request-stale-storage",
        )
    assert storage_blocked.value.code == "source_permission_request_stale"

    with pytest.raises(GovernanceNotFoundError):
        governance.service.list_evidence_versions(
            governance.reader,
            evidence_id,
        )

    with governance.database.session_factory() as session:
        row = session.get(EvidenceCardVersion, evidence_id)
        assert row is not None
        card = EvidenceCard.model_validate(row.payload)
        projection = ProjectionBuilder().from_evidence_card(card).model_dump(
            mode="json"
        )
        with pytest.raises(KnowledgeReadModelError, match="approval is invalid"):
            ProductionKnowledgeReadModel._require_current_evidence_approval(
                session,
                row,
                card,
                projection,
            )


def test_migrated_approval_exceeding_backfilled_request_fails_closed_everywhere(
    governance,
) -> None:
    source_version_id, evidence_id = _approved_evidence(
        governance,
        slug="permission-migration-ceiling",
        storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
    )
    with governance.database.session_factory() as session:
        session.execute(
            update(SourceReviewDecision)
            .where(SourceReviewDecision.source_version_id == source_version_id)
            .values(
                storage_permission=StoragePermission.DERIVED_WITH_SHORT_EXCERPT
            )
        )
        session.commit()

    with pytest.raises(GovernanceConflictError) as claim_blocked:
        governance.service.submit_claim(
            governance.operator,
            _claim_command(
                source_version_id,
                claim="A migrated approval must not exceed its backfilled request.",
            ),
            idempotency_key="permission-migration-ceiling-second-claim",
        )
    assert claim_blocked.value.code == "source_permission_request_ceiling_exceeded"

    mapping = ProductionMappingService(
        governance.database.session_factory,
        clock=lambda: NOW,
    )
    with pytest.raises(GovernanceConflictError) as mapping_blocked:
        mapping.submit_entity(
            governance.operator,
            _entity_command(evidence_id),
            idempotency_key="permission-migration-ceiling-mapping",
        )
    assert (
        mapping_blocked.value.code
        == "source_permission_request_ceiling_exceeded"
    )

    storage = _storage(governance)
    with pytest.raises(StorageConflictError) as storage_blocked:
        storage.build_manifest(
            governance.operator,
            _build_command(evidence_id=evidence_id),
            idempotency_key="permission-migration-ceiling-storage",
        )
    assert (
        storage_blocked.value.code
        == "source_permission_request_ceiling_exceeded"
    )

    with governance.database.session_factory() as session:
        row = session.get(EvidenceCardVersion, evidence_id)
        assert row is not None
        card = EvidenceCard.model_validate(row.payload)
        projection = ProjectionBuilder().from_evidence_card(card).model_dump(
            mode="json"
        )
        with pytest.raises(KnowledgeReadModelError, match="approval is invalid"):
            ProductionKnowledgeReadModel._require_current_evidence_approval(
                session,
                row,
                card,
                projection,
            )


def test_current_source_policy_is_rechecked_at_build_authorize_and_read(
    governance,
) -> None:
    source_version_id, evidence_id = _approved_evidence(
        governance, slug="current-source-policy"
    )
    storage = _storage(governance)
    command = _build_command(evidence_id=evidence_id)
    manifest = storage.build_manifest(
        governance.operator,
        command,
        idempotency_key="current-source-policy-build",
    )

    _append_incompatible_current_source_decision(governance, source_version_id)

    with pytest.raises(StorageConflictError) as authorization_error:
        storage.authorize_manifest(
            governance.operator,
            manifest.id,
            ManifestAuthorizationCommand(
                reason="Recheck every current approval before authorization."
            ),
            idempotency_key="current-source-policy-authorize",
        )
    assert authorization_error.value.code == "evidence_governance_mismatch"

    with pytest.raises(StorageConflictError) as build_error:
        storage.build_manifest(
            governance.operator,
            _build_command(evidence_id=evidence_id),
            idempotency_key="current-source-policy-second-build",
        )
    assert build_error.value.code == "evidence_governance_mismatch"

    with pytest.raises(StorageConflictError) as build_replay_error:
        storage.build_manifest(
            governance.operator,
            command,
            idempotency_key="current-source-policy-build",
        )
    assert build_replay_error.value.code == "evidence_governance_mismatch"

    with pytest.raises(StorageConflictError) as detail_error:
        storage.get_manifest(governance.operator, manifest.id)
    assert detail_error.value.code == "evidence_governance_mismatch"
    assert storage.list_manifests(
        governance.operator,
        status="pending",
        corpus_id=manifest.corpus_id,
    ) == []

    with governance.database.session_factory() as session:
        row = session.get(EvidenceCardVersion, evidence_id)
        assert row is not None
        card = EvidenceCard.model_validate(row.payload)
        projection = ProjectionBuilder().from_evidence_card(card).model_dump(
            mode="json"
        )
        with pytest.raises(KnowledgeReadModelError):
            ProductionKnowledgeReadModel._require_current_evidence_approval(
                session, row, card, projection
            )


def test_current_source_policy_is_rechecked_before_ingestion_starts(
    governance,
) -> None:
    source_version_id, evidence_id = _approved_evidence(
        governance, slug="current-source-policy-ingest"
    )
    writer = _UnexpectedWriter()
    storage = ProductionStorageWorkflowService(
        governance.database.session_factory,
        writer=writer,
        allow_temporary_qdrant_writes=True,
        persistent_production_write_approved=False,
        vector_size=3,
        vector_distance="cosine",
        clock=lambda: NOW,
    )
    manifest = storage.build_manifest(
        governance.operator,
        _build_command(evidence_id=evidence_id),
        idempotency_key="current-source-policy-ingest-build",
    )
    authorization_command = ManifestAuthorizationCommand(
        reason="Authorize the exact approved artifact before the policy change."
    )
    authorized = storage.authorize_manifest(
        governance.operator,
        manifest.id,
        authorization_command,
        idempotency_key="current-source-policy-ingest-authorize",
    )
    assert authorized.status == "authorized"

    _append_incompatible_current_source_decision(governance, source_version_id)

    with pytest.raises(StorageConflictError) as authorization_replay:
        storage.authorize_manifest(
            governance.operator,
            manifest.id,
            authorization_command,
            idempotency_key="current-source-policy-ingest-authorize",
        )
    assert authorization_replay.value.code == "evidence_governance_mismatch"

    with pytest.raises(StorageConflictError) as blocked:
        storage.ingest_manifest(
            governance.operator,
            manifest.id,
            ManifestIngestionCommand(
                reason="This must fail before an ingestion operation is recorded."
            ),
            idempotency_key="current-source-policy-ingest-blocked",
        )
    assert blocked.value.code == "evidence_governance_mismatch"
    assert writer.write_calls == 0
    with governance.database.session_factory() as session:
        manifest_row = session.get(StorageManifestRecord, manifest.id)
        assert manifest_row is not None
        assert manifest_row.status == StorageManifestState.AUTHORIZED
        assert session.scalar(select(IngestionOperationRecord)) is None
        assert session.scalar(select(IngestionReceiptRecord)) is None


def test_current_approval_change_during_writer_is_cleaned_before_receipt(
    governance,
) -> None:
    source_version_id, evidence_id = _approved_evidence(
        governance, slug="approval-race-during-write"
    )
    writer = FakeWriter(
        after_write=lambda: _append_incompatible_current_source_decision(
            governance, source_version_id
        )
    )
    storage = ProductionStorageWorkflowService(
        governance.database.session_factory,
        writer=writer,
        allow_temporary_qdrant_writes=True,
        vector_size=3,
        vector_distance="cosine",
        clock=lambda: NOW,
    )
    manifest = storage.build_manifest(
        governance.operator,
        _build_command(evidence_id=evidence_id),
        idempotency_key="approval-race-build",
    )
    storage.authorize_manifest(
        governance.operator,
        manifest.id,
        ManifestAuthorizationCommand(reason="Authorize before the simulated race."),
        idempotency_key="approval-race-authorize",
    )

    with pytest.raises(StorageConflictError) as failed:
        storage.ingest_manifest(
            governance.operator,
            manifest.id,
            ManifestIngestionCommand(
                reason="Simulate approval revocation after the external vector write."
            ),
            idempotency_key="approval-race-ingest",
        )
    assert failed.value.code == "ingestion_finalize_failed"
    assert writer.write_calls == 1
    assert writer.cleanup_calls == 1
    with governance.database.session_factory() as session:
        operation = session.scalar(select(IngestionOperationRecord))
        assert operation.error_code == "evidence_governance_mismatch"
        assert session.scalar(select(IngestionReceiptRecord)) is None
        assert session.get(StorageManifestRecord, manifest.id).status == StorageManifestState.AUTHORIZED


def test_derived_only_evidence_redacts_projection_and_search_locator(
    governance,
) -> None:
    _source_version_id, evidence_id = _approved_evidence(
        governance,
        slug="derived-only-locator",
        storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
    )
    storage = _storage(governance)
    built = storage.build_manifest(
        governance.operator,
        _build_command(evidence_id=evidence_id),
        idempotency_key="derived-only-locator-build",
    )

    with governance.database.session_factory() as session:
        manifest_row = session.get(StorageManifestRecord, built.id)
        evidence_row = session.get(EvidenceCardVersion, evidence_id)
        assert manifest_row is not None and evidence_row is not None
        manifest = StorageManifest.model_validate(manifest_row.manifest_payload)
        projection = manifest.projections[0]
        assert projection.source_locator == "withheld by storage policy"
        assert projection.page_number is None

        card = EvidenceCard.model_validate(evidence_row.payload)
        ProductionKnowledgeReadModel._require_current_evidence_approval(
            session,
            evidence_row,
            card,
            projection.to_payload(
                corpus_id=manifest.corpus_id,
                manifest_id=manifest.id,
                manifest_hash=manifest.manifest_hash,
            ),
        )
        result_payload = _safe_search_projection(
            _LoadedArtifact(
                value=card,
                projection=projection.model_dump(mode="json"),
            )
        )
        assert "source_locator" not in result_payload
        assert "page_number" not in result_payload


def test_inactive_canonical_entity_blocks_node_build_authorize_and_read(
    governance,
) -> None:
    _source_version_id, evidence_id = _approved_evidence(
        governance, slug="canonical-entity-status"
    )
    mapping = ProductionMappingService(
        governance.database.session_factory, clock=lambda: NOW
    )
    proposal = mapping.submit_entity(
        governance.operator,
        _entity_command(evidence_id),
        idempotency_key="canonical-entity-status-submit",
    )
    approval = mapping.review_mapping(
        governance.reviewer,
        proposal.id,
        _approve(governance.reviewer),
        idempotency_key="canonical-entity-status-review",
    )
    assert approval.knowledge_node_version_id is not None

    storage = _storage(governance)
    command = _build_command(
        evidence_id=evidence_id,
        node_id=approval.knowledge_node_version_id,
    )
    manifest = storage.build_manifest(
        governance.operator,
        command,
        idempotency_key="canonical-entity-status-build",
    )

    with governance.database.session_factory() as session:
        node_row = session.get(
            KnowledgeNodeVersionRecord, approval.knowledge_node_version_id
        )
        assert node_row is not None
        entity = session.get(KnowledgeEntityRecord, node_row.entity_id)
        assert entity is not None
        replacement_id = uuid4()
        session.add(
            KnowledgeEntityRecord(
                id=replacement_id,
                entity_type=entity.entity_type,
                canonical_name="Selective Attention",
                canonical_key=f"{entity.entity_type.value}:selective-attention",
                aliases=[],
                description="An active replacement identity used by this test.",
                attributes={},
                status=CanonicalEntityStatus.ACTIVE,
                created_by_user_id=governance.operator.user_id,
                created_at=NOW + timedelta(seconds=1),
                updated_at=NOW + timedelta(seconds=1),
            )
        )
        session.flush()
        entity.status = CanonicalEntityStatus.MERGED
        entity.merged_into_entity_id = replacement_id
        entity.updated_at = NOW + timedelta(seconds=1)
        session.commit()

    with pytest.raises(StorageConflictError) as authorization_error:
        storage.authorize_manifest(
            governance.operator,
            manifest.id,
            ManifestAuthorizationCommand(
                reason="Require every Knowledge Node identity to remain canonical."
            ),
            idempotency_key="canonical-entity-status-authorize",
        )
    assert authorization_error.value.code == "canonical_entity_inactive"

    with pytest.raises(StorageConflictError) as build_error:
        storage.build_manifest(
            governance.operator,
            _build_command(
                evidence_id=evidence_id,
                node_id=approval.knowledge_node_version_id,
            ),
            idempotency_key="canonical-entity-status-second-build",
        )
    assert build_error.value.code == "canonical_entity_inactive"

    with governance.database.session_factory() as session:
        node_row = session.get(
            KnowledgeNodeVersionRecord, approval.knowledge_node_version_id
        )
        assert node_row is not None
        node = KnowledgeNodeVersion.model_validate(node_row.payload)
        with pytest.raises(KnowledgeReadModelError):
            ProductionKnowledgeReadModel._require_current_mapping_approval(
                session,
                node_row,
                value=node,
                relationship=False,
            )


def test_storage_rejects_mapping_evidence_exact_version_substitution(
    governance,
) -> None:
    _source_version_id, evidence_id = _approved_evidence(
        governance, slug="mapping-exact-evidence"
    )
    _other_source_version_id, other_evidence_id = _approved_evidence(
        governance, slug="mapping-substituted-evidence"
    )
    mapping = ProductionMappingService(
        governance.database.session_factory, clock=lambda: NOW
    )
    proposal = mapping.submit_entity(
        governance.operator,
        _entity_command(evidence_id),
        idempotency_key="mapping-exact-submit",
    )
    approval = mapping.review_mapping(
        governance.reviewer,
        proposal.id,
        _approve(governance.reviewer),
        idempotency_key="mapping-exact-review",
    )
    assert approval.knowledge_node_version_id is not None

    with governance.database.session_factory() as session:
        substituted = session.get(EvidenceCardVersion, other_evidence_id)
        assert substituted is not None
        session.execute(
            update(MappingProposalEvidence)
            .where(MappingProposalEvidence.mapping_proposal_id == proposal.id)
            .values(
                evidence_card_version_id=substituted.id,
                evidence_domain_object_id=substituted.domain_object_id,
                evidence_payload_checksum=substituted.payload_checksum,
            )
        )
        session.commit()

    storage = _storage(governance)
    with pytest.raises(StorageConflictError) as changed:
        storage.build_manifest(
            governance.operator,
            _build_command(
                evidence_id=evidence_id,
                node_id=approval.knowledge_node_version_id,
            ),
            idempotency_key="mapping-exact-build",
        )
    assert changed.value.code == "mapping_evidence_binding_changed"


def test_eligible_artifact_scan_is_bounded_and_never_returns_a_hidden_hole(
    governance,
    monkeypatch,
) -> None:
    _valid_source, valid_evidence_id = _approved_evidence(
        governance, slug="bounded-scan-valid"
    )
    stale_source, stale_evidence_id = _approved_evidence(
        governance, slug="bounded-scan-stale"
    )
    _append_incompatible_current_source_decision(governance, stale_source)
    with governance.database.session_factory() as session:
        session.execute(
            update(EvidenceCardVersion)
            .where(EvidenceCardVersion.id == valid_evidence_id)
            .values(created_at=NOW)
        )
        session.execute(
            update(EvidenceCardVersion)
            .where(EvidenceCardVersion.id == stale_evidence_id)
            .values(created_at=NOW + timedelta(minutes=1))
        )
        session.commit()

    storage = _storage(governance)
    resolver = storage._artifact_resolver
    calls = 0

    def counting_resolver(session, command, actor):
        nonlocal calls
        calls += 1
        return resolver(session, command, actor)

    storage._artifact_resolver = counting_resolver
    monkeypatch.setattr(storage_workflow_module, "ELIGIBLE_ARTIFACT_SCAN_LIMIT", 1)
    with pytest.raises(StorageConflictError) as bounded:
        storage.list_eligible_artifacts(
            governance.operator,
            artifact_type="evidence_card",
            limit=1,
        )
    assert bounded.value.code == "eligible_artifact_scan_limit_exceeded"
    assert calls == 1

    calls = 0
    monkeypatch.setattr(storage_workflow_module, "ELIGIBLE_ARTIFACT_SCAN_LIMIT", 2)
    page = storage.list_eligible_artifacts(
        governance.operator,
        artifact_type="evidence_card",
        limit=1,
    )
    assert [item.artifact_row_id for item in page] == [valid_evidence_id]
    # One failed batch is bisected to isolate the stale row; valid neighbours
    # remain discoverable without silently shortening the page.
    assert calls == 3


def test_eligible_artifact_discovery_batches_healthy_catalogue_rows(
    governance,
) -> None:
    _first_source, first_evidence_id = _approved_evidence(
        governance, slug="batched-catalogue-first"
    )
    _second_source, second_evidence_id = _approved_evidence(
        governance, slug="batched-catalogue-second"
    )
    storage = _storage(governance)
    resolver = storage._artifact_resolver
    calls = 0

    def counting_resolver(session, command, actor):
        nonlocal calls
        calls += 1
        return resolver(session, command, actor)

    storage._artifact_resolver = counting_resolver
    page = storage.list_eligible_artifacts(
        governance.operator,
        artifact_type="evidence_card",
        limit=2,
    )

    assert {item.artifact_row_id for item in page} == {
        first_evidence_id,
        second_evidence_id,
    }
    assert calls == 1
    assert storage_workflow_module.ELIGIBLE_ARTIFACT_SCAN_LIMIT >= 10_000
    assert storage_workflow_module.ELIGIBLE_ARTIFACT_MAX_OFFSET >= 9_900
