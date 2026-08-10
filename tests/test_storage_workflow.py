"""Checkpoint 4 storage workflow tests; no persistent Qdrant is contacted."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from knowledge.evidence import (
    ClaimRole,
    ConfidenceLabel,
    ContradictionStatus,
    EvidenceClass,
    EvidenceLevel,
    KnowledgeOrigin,
    MagicDomain,
    SourceType,
)
from knowledge.governance import (
    ReviewStatus,
    SecretExposureLevel,
    SensitiveInformationLevel,
)
from knowledge.models import EntityType
from knowledge.projections import (
    ArtifactType,
    IngestionReceipt,
    KnowledgeType,
    QdrantProjection,
)
from persistence import (
    AuditEvent,
    Base,
    DatabaseManager,
    Role,
    RoleName,
    SessionRecord,
    SessionStatus,
    SessionTransport,
    User,
    UserRole,
)
from persistence.storage_models import (
    ActiveCorpusPointer,
    CorpusActivationState,
    CorpusVersionRecord,
    IngestionOperationRecord,
    IngestionOperationState,
    IngestionReceiptRecord,
    StorageManifestItemRecord,
    StorageManifestRecord,
    StorageManifestState,
)
from research.review.storage_workflow_models import (
    CorpusActivationCommand,
    ManifestAuthorizationCommand,
    ManifestBuildCommand,
    ManifestIngestionCommand,
    PersistentProductionWriteCapability,
)
from research.review.storage_workflow_service import (
    ProductionStorageWorkflowService,
    ResolvedArtifact,
    ResolvedManifestArtifacts,
    StorageAuthorizationError,
    StorageConflictError,
)
from security.policy import AuthenticatedActor


NOW = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)


@dataclass
class Harness:
    database: DatabaseManager
    actor: AuthenticatedActor


@pytest.fixture
def harness(tmp_path):
    database = DatabaseManager(f"sqlite+pysqlite:///{tmp_path / 'storage.sqlite'}")
    database.start()
    Base.metadata.create_all(database.engine)
    user_id, session_id = uuid4(), uuid4()
    with database.session_factory() as session:
        operator = Role(name=RoleName.OPERATOR, description="operator")
        session.add(operator)
        session.flush()
        session.add(User(
            id=user_id, username="Operator One", username_normalized="operator one",
            email="operator@example.test", email_normalized="operator@example.test",
            password_hash="test-hash", created_at=NOW, updated_at=NOW,
            password_changed_at=NOW,
        ))
        session.flush()
        session.add(UserRole(
            user_id=user_id, role_id=operator.id, granted_by_user_id=user_id, granted_at=NOW,
        ))
        session.add(SessionRecord(
            id=session_id, user_id=user_id, token_hash="a" * 64,
            transport=SessionTransport.BEARER, status=SessionStatus.ACTIVE,
            expires_at=NOW + timedelta(hours=2), created_at=NOW,
        ))
        session.commit()
    actor = AuthenticatedActor(
        user_id=user_id, username="Operator One", roles=frozenset({RoleName.OPERATOR}),
        session_id=session_id, session_transport=SessionTransport.BEARER,
        session_expires_at=NOW + timedelta(hours=2),
    )
    try:
        yield Harness(database, actor)
    finally:
        database.close()


def _projection() -> QdrantProjection:
    artifact_id, source_id = str(uuid4()), str(uuid4())
    return QdrantProjection(
        knowledge_unit_id=QdrantProjection.id_for_artifact(artifact_id, 1),
        artifact_type=ArtifactType.EVIDENCE_CARD,
        artifact_id=artifact_id,
        artifact_version=1,
        knowledge_type=KnowledgeType.EVIDENCE,
        text="Knowledge type: evidence\nClaim: Attention may limit reporting.",
        title="Attention may limit reporting",
        domain=[MagicDomain.THEORY],
        ontology_paths=["psychology.attention"],
        knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        evidence_level=EvidenceLevel.REVIEW,
        evidence_class=EvidenceClass.SYSTEMATIC_REVIEW,
        claim_roles=[ClaimRole.RESULT],
        confidence=0.8,
        confidence_label=ConfidenceLabel.HIGH,
        limitations=["Context dependent."],
        source_type=SourceType.JOURNAL_ARTICLE,
        source_id=source_id,
        citation_id=str(uuid4()),
        source_candidate_id=str(uuid4()),
        document_id=str(uuid4()),
        source_locator="page 7",
        evidence_card_id=artifact_id,
        canonical_claim_id=str(uuid4()),
        supporting_evidence_ids=[artifact_id],
        contradiction_status=ContradictionStatus.NONE_FOUND,
        entity_ids=[source_id],
        entity_types=[EntityType.SOURCE],
        secret_exposure_level=SecretExposureLevel.GENERAL_PRINCIPLE,
        secret_exposure_rank=1,
        sensitive_information_level=SensitiveInformationLevel.CONTROLLED,
        review_status=ReviewStatus.INGESTED,
        review_item_id=str(uuid4()),
        claim_review_item_ids=[str(uuid4())],
        reviewed_at=NOW,
    )


class StaticResolver:
    def __init__(self, projection=None):
        self.projection = projection or _projection()
        self.calls = 0
        self.row_id = uuid4()

    def __call__(self, _session, _command, _actor):
        self.calls += 1
        return ResolvedManifestArtifacts(
            artifacts=(ResolvedArtifact(
                artifact_type="evidence_card", row_id=self.row_id,
                domain_id=UUID(self.projection.artifact_id), version=1,
                payload_checksum="b" * 64,
                sensitivity=SensitiveInformationLevel.CONTROLLED,
                projection=self.projection,
            ),),
            validation_results={"passed": True, "run": self.calls},
        )


class FakeWriter:
    def __init__(
        self,
        *,
        fail=False,
        cleanup="cleaned",
        bad_checksums=False,
        after_write=None,
    ):
        self.fail = fail
        self.cleanup = cleanup
        self.bad_checksums = bad_checksums
        self.after_write = after_write
        self.write_calls = 0
        self.cleanup_calls = 0

    def write_manifest(self, manifest, *, actor):
        self.write_calls += 1
        if self.fail:
            raise RuntimeError("isolated writer failure")
        checksums = {
            item.knowledge_unit_id: item.payload_checksum
            for item in manifest.projections
        }
        if self.bad_checksums:
            checksums = {point_id: "0" * 64 for point_id in checksums}
        receipt = IngestionReceipt(
            manifest_id=manifest.id, manifest_hash=manifest.manifest_hash,
            corpus_id=manifest.corpus_id, collection_name=manifest.collection_name,
            point_ids=manifest.expected_point_ids,
            payload_checksums=checksums,
            actor=actor, ingested_at=NOW,
        )
        if self.after_write is not None:
            self.after_write()
        return receipt

    def cleanup_manifest_points(self, _manifest):
        self.cleanup_calls += 1
        return self.cleanup


def _service(
    harness,
    resolver,
    writer=None,
    *,
    temporary=True,
    persistent=False,
    capability=None,
):
    return ProductionStorageWorkflowService(
        harness.database.session_factory, artifact_resolver=resolver, writer=writer,
        allow_temporary_qdrant_writes=temporary,
        persistent_production_write_approved=persistent,
        persistent_write_capability=capability,
        vector_size=3, vector_distance="cosine", clock=lambda: NOW,
    )


def _build_and_authorize(service, actor, *, collection="magicforge_cp4_test_unit"):
    corpus_id = uuid4()
    built = service.build_manifest(
        actor,
        ManifestBuildCommand(
            corpus_id=corpus_id, collection_name=collection,
            evidence_version_ids=[uuid4()],
        ),
        idempotency_key=f"build-{corpus_id}",
    )
    authorized = service.authorize_manifest(
        actor, built.id,
        ManifestAuthorizationCommand(reason="Exact approved versions and gates checked."),
        idempotency_key=f"authorize-{corpus_id}",
    )
    return authorized


def test_manifest_ingestion_and_activation_are_separate_audited_idempotent_steps(harness):
    resolver, writer = StaticResolver(), FakeWriter()
    service = _service(harness, resolver, writer)
    manifest = _build_and_authorize(service, harness.actor)

    command = ManifestIngestionCommand(reason="Write isolated manifest to temporary Qdrant.")
    ingested = service.ingest_manifest(
        harness.actor, manifest.id, command, idempotency_key="ingest-one"
    )
    replay = service.ingest_manifest(
        harness.actor, manifest.id, command, idempotency_key="ingest-one"
    )
    assert replay == ingested
    assert writer.write_calls == 1
    with pytest.raises(StorageConflictError, match="idempotency key was reused"):
        service.ingest_manifest(
            harness.actor,
            manifest.id,
            ManifestIngestionCommand(reason="Changed content after completion."),
            idempotency_key="ingest-one",
        )
    post_ingest = ManifestIngestionCommand(
        reason="Bind a fresh replay key after ingestion."
    )
    assert service.ingest_manifest(
        harness.actor,
        manifest.id,
        post_ingest,
        idempotency_key="post-ingest-replay",
    ) == ingested
    with pytest.raises(StorageConflictError, match="idempotency key was reused"):
        service.ingest_manifest(
            harness.actor,
            manifest.id,
            ManifestIngestionCommand(reason="Changed post-ingest replay content."),
            idempotency_key="post-ingest-replay",
        )

    active = service.activate_corpus(
        harness.actor, manifest.corpus_id,
        CorpusActivationCommand(reason="Activate the receipt-backed isolated corpus."),
        idempotency_key="activate-one",
    )
    assert active.activation_state == "active"
    assert active.manifest_id == manifest.id

    with harness.database.session_factory() as session:
        manifest_row = session.get(StorageManifestRecord, manifest.id)
        corpus = session.get(CorpusVersionRecord, manifest.corpus_id)
        pointer = session.get(ActiveCorpusPointer, "production")
        events = list(session.scalars(select(AuditEvent.event_type)))
        item = session.scalar(select(StorageManifestItemRecord))
        assert manifest_row.status == StorageManifestState.INGESTED
        assert corpus.activation_state == CorpusActivationState.ACTIVE
        assert pointer.corpus_id == manifest.corpus_id
        assert item.payload_checksum == "b" * 64  # immutable domain artifact payload
        assert item.projection_checksum == resolver.projection.payload_checksum
        assert "storage_manifest_authorized" in events
        assert "manifest_ingestion_succeeded" in events
        assert "production_corpus_activated" in events


def test_persistent_production_write_requires_separate_approval(harness):
    resolver, writer = StaticResolver(), FakeWriter()
    service = _service(harness, resolver, writer, persistent=False)
    manifest = _build_and_authorize(
        service, harness.actor, collection="magicforge_knowledge_v01"
    )
    with pytest.raises(StorageConflictError, match="separate explicit approval"):
        service.ingest_manifest(
            harness.actor, manifest.id,
            ManifestIngestionCommand(reason="Attempt a persistent write."),
            idempotency_key="persistent-write",
        )
    assert writer.write_calls == 0
    with harness.database.session_factory() as session:
        assert session.scalar(select(IngestionOperationRecord)) is None


def test_manifest_detail_list_and_build_replay_recheck_current_clearance(harness):
    with harness.database.session_factory() as session:
        reviewer = Role(name=RoleName.REVIEWER, description="reviewer clearance")
        session.add(reviewer)
        session.flush()
        assignment = UserRole(
            user_id=harness.actor.user_id,
            role_id=reviewer.id,
            granted_by_user_id=harness.actor.user_id,
            granted_at=NOW,
        )
        session.add(assignment)
        session.commit()
        reviewer_role_id = reviewer.id

    resolver = StaticResolver()
    service = _service(harness, resolver, FakeWriter())
    corpus_id = uuid4()
    command = ManifestBuildCommand(
        corpus_id=corpus_id,
        collection_name="magicforge_cp4_test_clearance",
        evidence_version_ids=[uuid4()],
        authorized_sensitive_levels=[
            SensitiveInformationLevel.PUBLIC,
            SensitiveInformationLevel.CONTROLLED,
            SensitiveInformationLevel.SECRET_METHOD,
        ],
    )
    manifest = service.build_manifest(
        harness.actor,
        command,
        idempotency_key="clearance-build",
    )
    with harness.database.session_factory() as session:
        assignment = session.get(
            UserRole, (harness.actor.user_id, reviewer_role_id)
        )
        assignment.revoked_at = NOW + timedelta(seconds=1)
        assignment.revoked_by_user_id = harness.actor.user_id
        session.commit()

    with pytest.raises(StorageAuthorizationError):
        service.get_manifest(harness.actor, manifest.id)
    assert service.list_manifests(harness.actor, corpus_id=corpus_id) == []
    with pytest.raises(StorageAuthorizationError):
        service.build_manifest(
            harness.actor,
            command,
            idempotency_key="clearance-build",
        )


def test_persistent_write_requires_and_matches_exact_manifest_fingerprint(harness):
    resolver, writer = StaticResolver(), FakeWriter()
    builder = _service(harness, resolver, writer, persistent=False)
    manifest = _build_and_authorize(
        builder, harness.actor, collection="magicforge_knowledge_v01"
    )

    missing = _service(harness, resolver, writer, persistent=True)
    with pytest.raises(StorageConflictError) as missing_error:
        missing.ingest_manifest(
            harness.actor,
            manifest.id,
            ManifestIngestionCommand(reason="Global switch alone is insufficient."),
            idempotency_key="persistent-missing-fingerprint",
        )
    assert missing_error.value.code == "persistent_write_fingerprint_mismatch"

    wrong = _service(
        harness,
        resolver,
        writer,
        persistent=True,
        capability=PersistentProductionWriteCapability(
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            collection_name=manifest.collection_name,
            point_count=manifest.expected_point_count + 1,
        ),
    )
    with pytest.raises(StorageConflictError) as wrong_error:
        wrong.ingest_manifest(
            harness.actor,
            manifest.id,
            ManifestIngestionCommand(reason="Reject a mismatched exact capability."),
            idempotency_key="persistent-wrong-fingerprint",
        )
    assert wrong_error.value.code == "persistent_write_fingerprint_mismatch"
    assert writer.write_calls == 0

    exact = _service(
        harness,
        resolver,
        writer,
        persistent=True,
        capability=PersistentProductionWriteCapability(
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            collection_name=manifest.collection_name,
            point_count=manifest.expected_point_count,
        ),
    )
    result = exact.ingest_manifest(
        harness.actor,
        manifest.id,
        ManifestIngestionCommand(reason="Exercise an exact fake-writer capability."),
        idempotency_key="persistent-exact-fingerprint",
    )
    assert result.manifest_id == manifest.id
    assert writer.write_calls == 1


@pytest.mark.parametrize(
    ("cleanup", "expected_state"),
    [
        ("cleaned", IngestionOperationState.FAILED_CLEANED),
        ("reconciliation_required", IngestionOperationState.RECONCILIATION_REQUIRED),
    ],
)
def test_failed_ingestion_never_creates_receipt_or_activatable_corpus(
    harness, cleanup, expected_state
):
    resolver, writer = StaticResolver(), FakeWriter(fail=True, cleanup=cleanup)
    service = _service(harness, resolver, writer)
    manifest = _build_and_authorize(service, harness.actor)
    with pytest.raises(StorageConflictError, match="not ingested"):
        service.ingest_manifest(
            harness.actor, manifest.id,
            ManifestIngestionCommand(reason="Exercise isolated failure handling."),
            idempotency_key=f"failure-{cleanup}",
        )
    with harness.database.session_factory() as session:
        operation = session.scalar(select(IngestionOperationRecord))
        manifest_row = session.get(StorageManifestRecord, manifest.id)
        assert operation.state == expected_state
        assert manifest_row.status == StorageManifestState.AUTHORIZED
        assert session.scalar(select(IngestionReceiptRecord)) is None
        assert session.scalar(select(CorpusVersionRecord)) is None
    with pytest.raises(StorageConflictError, match="not ingested"):
        service.activate_corpus(
            harness.actor, manifest.corpus_id,
            CorpusActivationCommand(reason="Must not activate a failed ingestion."),
            idempotency_key=f"activate-failed-{cleanup}",
        )


def test_authorization_rebuilds_manifest_and_detects_checksum_change(harness):
    resolver = StaticResolver()
    service = _service(harness, resolver, FakeWriter())
    corpus_id = uuid4()
    built = service.build_manifest(
        harness.actor,
        ManifestBuildCommand(
            corpus_id=corpus_id, collection_name="magicforge_cp4_test_checksum",
            evidence_version_ids=[uuid4()],
        ),
        idempotency_key="build-checksum",
    )
    resolver.projection = _projection()
    with pytest.raises(StorageConflictError, match="no longer matches"):
        service.authorize_manifest(
            harness.actor, built.id,
            ManifestAuthorizationCommand(reason="Rerun all deterministic gates."),
            idempotency_key="authorize-checksum",
        )
    with harness.database.session_factory() as session:
        assert session.get(StorageManifestRecord, built.id).status == StorageManifestState.PENDING


def test_manifest_allows_only_one_active_or_reconciliation_operation(harness):
    resolver, writer = StaticResolver(), FakeWriter()
    service = _service(harness, resolver, writer)
    manifest = _build_and_authorize(service, harness.actor)
    with harness.database.session_factory() as session:
        session.add(IngestionOperationRecord(
            manifest_id=manifest.id,
            actor_user_id=harness.actor.user_id,
            idempotency_key="already-running",
            request_hash="c" * 64,
            state=IngestionOperationState.RUNNING,
            started_at=NOW,
        ))
        session.commit()

    with pytest.raises(StorageConflictError, match="already has an active ingestion"):
        service.ingest_manifest(
            harness.actor,
            manifest.id,
            ManifestIngestionCommand(reason="A competing operation must be rejected."),
            idempotency_key="second-operation",
        )
    assert writer.write_calls == 0
    assert writer.cleanup_calls == 0
    with harness.database.session_factory() as session:
        session.add(IngestionOperationRecord(
            manifest_id=manifest.id,
            actor_user_id=harness.actor.user_id,
            idempotency_key="database-race",
            request_hash="d" * 64,
            state=IngestionOperationState.RUNNING,
            started_at=NOW,
        ))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_reconciliation_state_blocks_new_ingestion_and_cleanup(harness):
    resolver = StaticResolver()
    writer = FakeWriter(fail=True, cleanup="reconciliation_required")
    service = _service(harness, resolver, writer)
    manifest = _build_and_authorize(service, harness.actor)
    with pytest.raises(StorageConflictError, match="not ingested"):
        service.ingest_manifest(
            harness.actor,
            manifest.id,
            ManifestIngestionCommand(reason="Create a reconciliation marker."),
            idempotency_key="first-failure",
        )
    with pytest.raises(StorageConflictError, match="requires reconciliation"):
        service.ingest_manifest(
            harness.actor,
            manifest.id,
            ManifestIngestionCommand(reason="Must wait for reconciliation."),
            idempotency_key="retry-before-reconciliation",
        )
    assert writer.write_calls == 1
    assert writer.cleanup_calls == 1


def test_receipt_payload_checksums_must_match_every_manifest_projection(harness):
    resolver = StaticResolver()
    writer = FakeWriter(bad_checksums=True)
    service = _service(harness, resolver, writer)
    manifest = _build_and_authorize(service, harness.actor)
    with pytest.raises(StorageConflictError, match="not ingested"):
        service.ingest_manifest(
            harness.actor,
            manifest.id,
            ManifestIngestionCommand(reason="Reject a forged receipt checksum."),
            idempotency_key="bad-receipt",
        )
    with harness.database.session_factory() as session:
        assert session.get(StorageManifestRecord, manifest.id).status == StorageManifestState.AUTHORIZED
        assert session.scalar(select(IngestionReceiptRecord)) is None
    assert writer.cleanup_calls == 1


def test_finalize_reconciles_manifest_again_after_external_write(harness):
    resolver = StaticResolver()
    writer = FakeWriter(after_write=lambda: setattr(resolver, "projection", _projection()))
    service = _service(harness, resolver, writer)
    manifest = _build_and_authorize(service, harness.actor)

    with pytest.raises(StorageConflictError) as changed:
        service.ingest_manifest(
            harness.actor,
            manifest.id,
            ManifestIngestionCommand(
                reason="Detect an approval or projection change during the external write."
            ),
            idempotency_key="post-write-reconcile",
        )
    assert changed.value.code == "ingestion_finalize_failed"
    assert writer.write_calls == 1
    assert writer.cleanup_calls == 1
    with harness.database.session_factory() as session:
        operation = session.scalar(select(IngestionOperationRecord))
        assert operation.state == IngestionOperationState.FAILED_CLEANED
        assert operation.error_code == "manifest_changed_during_ingestion"
        assert session.scalar(select(IngestionReceiptRecord)) is None
        assert session.scalar(select(CorpusVersionRecord)) is None
        assert session.get(StorageManifestRecord, manifest.id).status == StorageManifestState.AUTHORIZED


def test_activation_revalidates_canonical_receipt_checksum(harness):
    resolver, writer = StaticResolver(), FakeWriter()
    service = _service(harness, resolver, writer)
    manifest = _build_and_authorize(service, harness.actor)
    service.ingest_manifest(
        harness.actor,
        manifest.id,
        ManifestIngestionCommand(reason="Create receipt for tamper test."),
        idempotency_key="ingest-tamper-receipt",
    )
    with harness.database.session_factory() as session:
        session.execute(
            update(IngestionReceiptRecord).values(payload_checksum="0" * 64)
        )
        session.commit()

    with pytest.raises(StorageConflictError, match="receipt checksum is invalid"):
        service.activate_corpus(
            harness.actor,
            manifest.corpus_id,
            CorpusActivationCommand(reason="Receipt must be revalidated."),
            idempotency_key="activate-tampered-receipt",
        )


def test_activation_revalidates_full_corpus_manifest_identity(harness):
    resolver, writer = StaticResolver(), FakeWriter()
    service = _service(harness, resolver, writer)
    manifest = _build_and_authorize(service, harness.actor)
    service.ingest_manifest(
        harness.actor,
        manifest.id,
        ManifestIngestionCommand(reason="Create corpus for identity test."),
        idempotency_key="ingest-tamper-corpus",
    )
    with harness.database.session_factory() as session:
        session.execute(
            update(CorpusVersionRecord).values(qdrant_collection="wrong_collection")
        )
        session.commit()

    with pytest.raises(StorageConflictError, match="corpus identity"):
        service.activate_corpus(
            harness.actor,
            manifest.corpus_id,
            CorpusActivationCommand(reason="Corpus identity must be revalidated."),
            idempotency_key="activate-tampered-corpus",
        )
