"""Isolated tests for the Checkpoint 1 relational persistence core."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError

from persistence import (
    AppendOnlyMutationError,
    AuditEvent,
    AuditEventRepository,
    AuditMetadataError,
    Base,
    DatabaseInitializationError,
    DatabaseManager,
    DatabasePhase,
    EvidenceCardVersion,
    EXPECTED_DATABASE_REVISION,
    CandidateCreatedByKind,
    CitationVerificationEvidence,
    ClaimCandidate,
    ClaimReviewDecision,
    GovernanceWorkflowStatus,
    ReviewDecisionKind,
    RoleName,
    Source,
    SourceReviewDecision,
    SourceVersion,
    SqlAlchemyUnitOfWork,
    User,
)
from persistence.models import SourcePermissionRequest
from knowledge.evidence import (
    ClaimPolarity,
    ClaimRole,
    ContradictionStatus,
    EvidenceClass,
    EvidenceLevel,
    KnowledgeOrigin,
    SourceType,
)
from knowledge.governance import (
    ClaimEligibility,
    ContradictionCheckStatus,
    ExtractionPermission,
    SecretExposureLevel,
    SensitiveInformationLevel,
    StoragePermission,
)
from research.citation.models import VerificationMethod, VerificationScope
from research.models import ContentAccess


@pytest.fixture
def database(tmp_path):
    manager = DatabaseManager(
        f"sqlite+pysqlite:///{tmp_path / 'persistence-foundation.sqlite'}"
    )
    manager.start()
    assert manager.phase == DatabasePhase.READY
    # create_all is intentionally confined to this disposable unit-test DB;
    # Production schema management is Alembic-only.
    Base.metadata.create_all(manager.engine)
    try:
        yield manager
    finally:
        manager.close()


def _user(username: str = "Ryan") -> User:
    return User(
        username=username,
        username_normalized=username.casefold(),
        password_hash="$argon2id$unit-test-placeholder",
    )


def _governance_chain(database) -> dict[type, object]:
    submitter = _user("source-submitter")
    reviewer = _user("human-reviewer")
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        unit.session.add_all([submitter, reviewer])
        unit.flush()

        source = Source(
            canonical_key=f"doi:10.0000/{uuid4()}",
            submitted_by_user_id=submitter.id,
        )
        unit.session.add(source)
        unit.flush()

        source_version = SourceVersion(
            source_id=source.id,
            version_number=1,
            citation_id=uuid4(),
            citation_metadata={"title": "Attention in performance magic"},
            access_metadata={"license": "unit-test"},
            content_text="Participants sometimes missed the unexpected action.",
            content_hash="a" * 64,
            content_access=ContentAccess.FULL_TEXT,
            source_type=SourceType.JOURNAL_ARTICLE,
            knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
            sensitivity=SensitiveInformationLevel.PUBLIC,
            submitted_by_user_id=submitter.id,
        )
        unit.session.add(source_version)
        unit.flush()

        permission_request = SourcePermissionRequest(
            source_version_id=source_version.id,
            sequence_number=1,
            requested_extraction_permission=(
                ExtractionPermission.SELECTED_SECTIONS
            ),
            requested_storage_permission=(
                StoragePermission.DERIVED_KNOWLEDGE_ONLY
            ),
            requested_scope_locators=["results"],
            rights_basis="Unit-test licensed access.",
            rights_evidence=["license:unit-test"],
            reason="Request bounded extraction for persistence verification.",
            submitted_by_user_id=submitter.id,
            actor_role_snapshot=[RoleName.OPERATOR.value],
            request_checksum="1" * 64,
        )
        unit.session.add(permission_request)
        unit.flush()

        citation_evidence = CitationVerificationEvidence(
            source_version_id=source_version.id,
            citation_id=source_version.citation_id,
            verification_scope=VerificationScope.METADATA,
            verification_method=VerificationMethod.METADATA_RESOLVER,
            resolver_result={"matched": True},
            verified_identifier="doi:10.0000/unit-test",
            checked_locator="https://doi.org/10.0000/unit-test",
            reviewer_user_id=reviewer.id,
            evidence_checksum="b" * 64,
        )
        unit.session.add(citation_evidence)
        unit.flush()

        source_decision = SourceReviewDecision(
            source_version_id=source_version.id,
            source_permission_request_id=permission_request.id,
            sequence_number=1,
            decision=ReviewDecisionKind.APPROVED,
            resulting_status=GovernanceWorkflowStatus.APPROVED,
            reviewer_user_id=reviewer.id,
            actor_role_snapshot=[RoleName.REVIEWER.value],
            reason="Verified metadata and authorized bounded extraction.",
            citation_verification_evidence_id=citation_evidence.id,
            claim_eligibility=ClaimEligibility.ELIGIBLE,
            extraction_permission=ExtractionPermission.SELECTED_SECTIONS,
            storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
            sensitive_information_level=SensitiveInformationLevel.PUBLIC,
            contradicting_evidence_checked=(
                ContradictionCheckStatus.CHECKED_NONE_FOUND
            ),
            extraction_scope={"sections": ["results"]},
            decision_checksum="c" * 64,
        )
        unit.session.add(source_decision)

        claim = ClaimCandidate(
            source_version_id=source_version.id,
            canonical_claim_id=uuid4(),
            claim_text="Some spectators did not notice an unexpected action.",
            claim_role=ClaimRole.RESULT,
            claim_polarity=ClaimPolarity.SUPPORTS,
            locator={"section": "results", "paragraph": 2},
            extraction_provenance={"extractor": "unit-test"},
            model_run_metadata={},
            proposed_evidence_card={"schema_version": "evidence-0.2"},
            candidate_checksum="d" * 64,
            extraction_confidence=0.95,
            evidence_card_eligible=True,
            status=GovernanceWorkflowStatus.SUBMITTED,
            submitted_by_user_id=submitter.id,
            created_by_kind=CandidateCreatedByKind.PIPELINE,
        )
        unit.session.add(claim)
        unit.flush()

        claim_decision = ClaimReviewDecision(
            claim_candidate_id=claim.id,
            sequence_number=1,
            decision=ReviewDecisionKind.APPROVED,
            resulting_status=GovernanceWorkflowStatus.APPROVED,
            reviewer_user_id=reviewer.id,
            actor_role_snapshot=[RoleName.REVIEWER.value],
            reason="The atomic result is directly supported by the locator.",
            confidence={"label": "high", "score": 0.9},
            limitations=["Laboratory task may not generalize to every performance."],
            evidence_class=EvidenceClass.CONTROLLED_EXPERIMENT,
            knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
            evidence_level=EvidenceLevel.EMPIRICAL,
            claim_eligibility=ClaimEligibility.ELIGIBLE,
            storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
            contradiction_status=ContradictionStatus.NONE_FOUND,
            contradiction_check_status=ContradictionCheckStatus.CHECKED_NONE_FOUND,
            contradicting_evidence_ids=[],
            sensitive_information_level=SensitiveInformationLevel.PUBLIC,
            secret_exposure_level=SecretExposureLevel.GENERAL_PRINCIPLE,
            decision_checksum="e" * 64,
        )
        unit.session.add(claim_decision)
        unit.flush()

        evidence_domain_id = uuid4()
        evidence_version = EvidenceCardVersion(
            evidence_card_id=claim.id,
            domain_object_id=evidence_domain_id,
            version_number=1,
            claim_candidate_id=claim.id,
            claim_review_decision_id=claim_decision.id,
            source_id=source.id,
            source_version_id=source_version.id,
            citation_id=source_version.citation_id,
            canonical_claim_id=claim.canonical_claim_id,
            payload={
                "id": str(evidence_domain_id),
                "schema_version": "evidence-0.2",
            },
            payload_checksum="f" * 64,
            created_by_user_id=reviewer.id,
        )
        unit.session.add(evidence_version)
        unit.commit()

    return {
        SourceVersion: source_version.id,
        SourcePermissionRequest: permission_request.id,
        CitationVerificationEvidence: citation_evidence.id,
        SourceReviewDecision: source_decision.id,
        ClaimReviewDecision: claim_decision.id,
        EvidenceCardVersion: evidence_version.id,
        Source: source.id,
        ClaimCandidate: claim.id,
        User: submitter.id,
    }


def test_database_manager_is_fail_closed_and_safe() -> None:
    disabled = DatabaseManager("")
    disabled.start()
    assert disabled.phase == DatabasePhase.DISABLED
    assert disabled.readiness().code == "database_disabled"
    with pytest.raises(DatabaseInitializationError, match="not configured"):
        disabled.require_ready()

    def explode(_url, **_options):
        raise RuntimeError("postgresql://user:very-secret@example.test/db")

    failed = DatabaseManager("postgresql://configured", engine_factory=explode)
    failed.start()
    assert failed.phase == DatabasePhase.FAILED
    assert failed.failure is not None
    assert "very-secret" not in failed.failure.message


def test_database_manager_requires_the_expected_alembic_revision(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'revision.sqlite'}"

    missing = DatabaseManager(
        database_url,
        expected_revision=EXPECTED_DATABASE_REVISION,
    )
    missing.start()
    assert missing.phase == DatabasePhase.FAILED
    assert missing.readiness().code == "database_schema_not_ready"

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('old')")
        )
    engine.dispose()

    incompatible = DatabaseManager(
        database_url,
        expected_revision=EXPECTED_DATABASE_REVISION,
    )
    incompatible.start()
    assert incompatible.phase == DatabasePhase.FAILED
    assert incompatible.readiness().message.endswith("revision is incompatible.")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num = :revision"),
            {"revision": EXPECTED_DATABASE_REVISION},
        )
    engine.dispose()

    ready = DatabaseManager(
        database_url,
        expected_revision=EXPECTED_DATABASE_REVISION,
    )
    ready.start()
    assert ready.phase == DatabasePhase.READY
    ready.close()


def test_unit_of_work_requires_explicit_commit_and_rolls_back(database) -> None:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        unit.session.add(_user())

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 0

    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        unit.session.add(_user())
        unit.commit()

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1


def test_unique_and_foreign_key_constraints_are_active(database) -> None:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        unit.session.add(_user("Ryan"))
        unit.commit()

    with pytest.raises(IntegrityError):
        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            unit.session.add(_user("RYAN"))
            unit.commit()

    # Audit actor attribution cannot reference a user that does not exist.
    with pytest.raises(IntegrityError):
        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            AuditEventRepository(unit.session).append(
                event_type="unit_test",
                actor_user_id=uuid4(),
                actor_role_snapshot=[RoleName.ADMIN],
                object_type="test_object",
                object_id="object-1",
                reason="Exercise the foreign-key boundary.",
            )


def test_audit_events_are_append_only(database) -> None:
    actor = _user()
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        unit.session.add(actor)
        unit.flush()
        event = AuditEventRepository(unit.session).append(
            event_type="user_created",
            actor_user_id=actor.id,
            actor_role_snapshot=[RoleName.ADMIN],
            object_type="user",
            object_id=str(actor.id),
            new_state={"status": "active"},
            reason="Create the first explicitly provisioned user.",
            metadata={"channel": "admin_cli"},
        )
        event_id = event.event_id
        unit.commit()

    with pytest.raises(AppendOnlyMutationError):
        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            stored = unit.session.get(AuditEvent, event_id)
            assert stored is not None
            stored.reason = "Attempted rewrite"
            unit.commit()

    with pytest.raises(AppendOnlyMutationError):
        with SqlAlchemyUnitOfWork(database.session_factory) as unit:
            stored = unit.session.get(AuditEvent, event_id)
            assert stored is not None
            unit.session.delete(stored)
            unit.commit()


def test_governance_versions_and_decisions_are_immutable(database) -> None:
    identities = _governance_chain(database)
    mutation_cases = (
        (SourceVersion, "content_text", "Attempted source rewrite."),
        (CitationVerificationEvidence, "notes", "Attempted evidence rewrite."),
        (SourcePermissionRequest, "reason", "Attempted request rewrite."),
        (SourceReviewDecision, "reason", "Attempted decision rewrite."),
        (ClaimReviewDecision, "reason", "Attempted decision rewrite."),
        (EvidenceCardVersion, "payload_checksum", "0" * 64),
    )

    for model, field, value in mutation_cases:
        with pytest.raises(AppendOnlyMutationError, match="immutable"):
            with SqlAlchemyUnitOfWork(database.session_factory) as unit:
                stored = unit.session.get(model, identities[model])
                assert stored is not None
                setattr(stored, field, value)
                unit.commit()

        with pytest.raises(AppendOnlyMutationError, match="immutable"):
            with SqlAlchemyUnitOfWork(database.session_factory) as unit:
                stored = unit.session.get(model, identities[model])
                assert stored is not None
                unit.session.delete(stored)
                unit.commit()


def test_source_permission_request_requires_rights_evidence(database) -> None:
    identities = _governance_chain(database)
    with database.session_factory() as session:
        invalid = SourcePermissionRequest(
            source_version_id=identities[SourceVersion],
            sequence_number=2,
            requested_extraction_permission=ExtractionPermission.NONE,
            requested_storage_permission=StoragePermission.NONE,
            requested_scope_locators=[],
            rights_basis="No asserted rights.",
            rights_evidence=[],
            reason="Exercise the non-empty rights evidence boundary.",
            submitted_by_user_id=identities[User],
            actor_role_snapshot=[RoleName.OPERATOR.value],
            request_checksum="8" * 64,
            supersedes_request_id=identities[SourcePermissionRequest],
        )
        session.add(invalid)
        with pytest.raises(
            ValueError, match="rights_evidence must be a non-empty JSON array"
        ):
            session.commit()
        session.rollback()


def test_source_decision_permission_request_must_match_source_version(
    database,
) -> None:
    identities = _governance_chain(database)
    with database.session_factory() as session:
        reviewer = _user("permission-link-reviewer")
        session.add(reviewer)
        session.flush()
        source = Source(
            canonical_key=f"doi:10.0000/{uuid4()}",
            submitted_by_user_id=identities[User],
        )
        session.add(source)
        session.flush()
        version = SourceVersion(
            source_id=source.id,
            version_number=1,
            citation_id=uuid4(),
            citation_metadata={"title": "Composite permission link probe"},
            access_metadata={"license": "unit-test"},
            content_text="A different immutable Source version.",
            content_hash="7" * 64,
            content_access=ContentAccess.FULL_TEXT,
            source_type=SourceType.JOURNAL_ARTICLE,
            knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
            sensitivity=SensitiveInformationLevel.PUBLIC,
            submitted_by_user_id=identities[User],
        )
        session.add(version)
        session.flush()
        session.commit()

        session.add(
            SourcePermissionRequest(
                source_version_id=version.id,
                sequence_number=1,
                requested_extraction_permission=ExtractionPermission.NONE,
                requested_storage_permission=StoragePermission.NONE,
                requested_scope_locators=[],
                rights_basis="No cross-version supersession is permitted.",
                rights_evidence=["unit-test:cross-version-supersession"],
                reason="Exercise the composite self-reference boundary.",
                submitted_by_user_id=identities[User],
                actor_role_snapshot=[RoleName.OPERATOR.value],
                request_checksum="9" * 64,
                supersedes_request_id=identities[SourcePermissionRequest],
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        session.add(
            SourceReviewDecision(
                source_version_id=version.id,
                source_permission_request_id=identities[
                    SourcePermissionRequest
                ],
                sequence_number=1,
                decision=ReviewDecisionKind.REJECTED,
                resulting_status=GovernanceWorkflowStatus.REJECTED,
                reviewer_user_id=reviewer.id,
                actor_role_snapshot=[RoleName.REVIEWER.value],
                reason="The permission request belongs to another version.",
                citation_verification_evidence_id=None,
                claim_eligibility=ClaimEligibility.INELIGIBLE,
                extraction_permission=ExtractionPermission.NONE,
                storage_permission=StoragePermission.NONE,
                sensitive_information_level=SensitiveInformationLevel.PUBLIC,
                contradicting_evidence_checked=(
                    ContradictionCheckStatus.NOT_CHECKED
                ),
                extraction_scope={},
                decision_checksum="6" * 64,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_source_content_hash_is_unique_within_source(database) -> None:
    identities = _governance_chain(database)
    with database.session_factory() as session:
        original = session.get(SourceVersion, identities[SourceVersion])
        assert original is not None
        duplicate = SourceVersion(
            source_id=original.source_id,
            version_number=2,
            citation_id=uuid4(),
            citation_metadata={"title": "Metadata-only rewrite"},
            access_metadata={"license": "unit-test"},
            content_text=original.content_text,
            content_hash=original.content_hash,
            content_access=original.content_access,
            source_type=original.source_type,
            knowledge_origin=original.knowledge_origin,
            sensitivity=original.sensitivity,
            submitted_by_user_id=identities[User],
            supersedes_version_id=original.id,
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


def test_context_only_claim_cannot_be_evidence_eligible(database) -> None:
    identities = _governance_chain(database)
    with database.session_factory() as session:
        claim = ClaimCandidate(
            source_version_id=identities[SourceVersion],
            canonical_claim_id=uuid4(),
            claim_text="This sentence is context, not an evidentiary claim.",
            claim_role=ClaimRole.CONTEXT_ONLY,
            claim_polarity=ClaimPolarity.SUPPORTS,
            locator={"section": "introduction"},
            extraction_provenance={"extractor": "unit-test"},
            model_run_metadata={},
            proposed_evidence_card={},
            candidate_checksum="9" * 64,
            extraction_confidence=0.8,
            evidence_card_eligible=True,
            status=GovernanceWorkflowStatus.SUBMITTED,
            submitted_by_user_id=identities[User],
            created_by_kind=CandidateCreatedByKind.PIPELINE,
        )
        session.add(claim)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.parametrize(
    "field,value",
    [
        ("metadata", {"api_key": "must-not-persist"}),
        ("previous_state", {"password_hash": "must-not-persist"}),
        ("new_state", {"session_token": "must-not-persist"}),
    ],
)
def test_audit_repository_rejects_sensitive_payloads(database, field, value) -> None:
    actor = _user()
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        unit.session.add(actor)
        unit.flush()
        arguments = {
            "event_type": "unsafe_event",
            "actor_user_id": actor.id,
            "actor_role_snapshot": [RoleName.ADMIN],
            "object_type": "user",
            "object_id": str(actor.id),
            "reason": "The unsafe payload must fail before insertion.",
            field: value,
        }
        with pytest.raises(AuditMetadataError, match="credentials or secrets"):
            AuditEventRepository(unit.session).append(**arguments)


def test_audit_repository_rejects_unknown_role(database) -> None:
    actor = _user()
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        unit.session.add(actor)
        unit.flush()
        with pytest.raises(AuditMetadataError, match="unknown role"):
            AuditEventRepository(unit.session).append(
                event_type="unsafe_event",
                actor_user_id=actor.id,
                actor_role_snapshot=["superuser"],
                object_type="user",
                object_id=str(actor.id),
                reason="Unknown roles cannot enter immutable history.",
            )


def test_audit_repository_keyset_is_stable_for_identical_timestamps(database) -> None:
    actor = _user("audit-pagination-actor")
    shared_timestamp = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    event_ids = [uuid4() for _ in range(7)]

    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        unit.session.add(actor)
        unit.flush()
        for event_id in event_ids:
            unit.session.add(
                AuditEvent(
                    event_id=event_id,
                    event_type="pagination_probe",
                    actor_user_id=actor.id,
                    actor_role_snapshot=[RoleName.ADMIN.value],
                    object_type="audit_probe",
                    object_id=str(event_id),
                    previous_state=None,
                    new_state=None,
                    reason="Verify deterministic audit keyset pagination.",
                    correlation_id=str(event_id),
                    event_metadata={},
                    created_at=shared_timestamp,
                )
            )
        unit.commit()

    observed: list[UUID] = []
    before_created_at = None
    before_event_id = None
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        repository = AuditEventRepository(unit.session)
        while True:
            page, has_more = repository.list_page(
                event_type="pagination_probe",
                before_created_at=before_created_at,
                before_event_id=before_event_id,
                limit=3,
            )
            observed.extend(event.event_id for event in page)
            if not has_more:
                break
            before_created_at = page[-1].created_at
            before_event_id = page[-1].event_id

    assert observed == sorted(event_ids, reverse=True)
    assert len(observed) == len(set(observed)) == 7
