"""Persistence tests for governed Production extraction jobs and attempts."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import func, select, update

from knowledge.evidence import KnowledgeOrigin, SourceType
from knowledge.governance import (
    ClaimEligibility,
    ContradictionCheckStatus,
    ExtractionPermission,
    SensitiveInformationLevel,
    StoragePermission,
)
from persistence.database import DatabaseManager
from persistence.idempotency import canonical_payload_hash
from persistence.extraction_jobs import (
    ExtractionAuthorizationError,
    ExtractionIntegrityError,
    ExtractionJobRepository,
    ExtractionPersistenceError,
    ExtractionStateError,
)
from persistence.models import (
    AppendOnlyMutationError,
    Base,
    CitationVerificationEvidence,
    ExtractionAttempt,
    ExtractionAttemptStatus,
    ExtractionJob,
    ExtractionJobStatus,
    ExtractionProposalArtifact,
    GovernanceWorkflowStatus,
    ReviewDecisionKind,
    RoleName,
    Source,
    SourcePermissionRequest,
    SourceReviewDecision,
    SourceVersion,
    User,
)
from persistence.uow import SqlAlchemyUnitOfWork
from research.citation.models import VerificationMethod, VerificationScope
from research.models import ContentAccess
from research.review.source_permission_policy import permission_request_checksum_payload


@pytest.fixture
def database(tmp_path):
    manager = DatabaseManager(
        f"sqlite+pysqlite:///{tmp_path / 'extraction-jobs.sqlite'}"
    )
    manager.start()
    Base.metadata.create_all(manager.engine)
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture
def approved_chain(database):
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        operator = User(
            username="extraction-operator",
            username_normalized="extraction-operator",
            password_hash="$argon2id$not-a-real-hash",
        )
        reviewer = User(
            username="extraction-reviewer",
            username_normalized="extraction-reviewer",
            password_hash="$argon2id$not-a-real-hash",
        )
        unit.session.add_all([operator, reviewer])
        unit.flush()
        source = Source(
            canonical_key="doi:10.0000/extraction-job",
            submitted_by_user_id=operator.id,
        )
        unit.session.add(source)
        unit.flush()
        content_text = "A controlled source snapshot for extraction."
        content_hash = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
        version = SourceVersion(
            source_id=source.id,
            version_number=1,
            citation_id=uuid4(),
            citation_metadata={"title": "Attention in performance magic"},
            access_metadata={"license": "test"},
            content_text=content_text,
            content_hash=content_hash,
            content_access=ContentAccess.FULL_TEXT,
            source_type=SourceType.JOURNAL_ARTICLE,
            knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
            sensitivity=SensitiveInformationLevel.CONTROLLED,
            submitted_by_user_id=operator.id,
        )
        unit.session.add(version)
        unit.flush()
        permission_payload = permission_request_checksum_payload(
            source_version_id=version.id,
            sequence_number=1,
            requested_extraction_permission=ExtractionPermission.SELECTED_SECTIONS,
            requested_storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
            requested_scope_locators=["p. 4"],
            rights_basis="Test license permits bounded extraction.",
            rights_evidence=["license:test"],
            reason="Exercise exact Production extraction persistence.",
            submitted_by_user_id=operator.id,
            actor_role_snapshot=[RoleName.OPERATOR.value],
            supersedes_request_id=None,
        )
        permission_checksum = canonical_payload_hash(permission_payload)
        permission = SourcePermissionRequest(
            id=uuid5(
                NAMESPACE_URL,
                "magicforge:source-permission-request:"
                f"{version.id}:1:{permission_checksum}",
            ),
            source_version_id=version.id,
            sequence_number=1,
            requested_extraction_permission=ExtractionPermission.SELECTED_SECTIONS,
            requested_storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
            requested_scope_locators=["p. 4"],
            rights_basis="Test license permits bounded extraction.",
            rights_evidence=["license:test"],
            reason="Exercise exact Production extraction persistence.",
            submitted_by_user_id=operator.id,
            actor_role_snapshot=[RoleName.OPERATOR.value],
            request_checksum=permission_checksum,
        )
        unit.session.add(permission)
        unit.flush()
        citation = CitationVerificationEvidence(
            source_version_id=version.id,
            citation_id=version.citation_id,
            verification_scope=VerificationScope.FULL_TEXT,
            verification_method=VerificationMethod.FULL_TEXT_LOCATOR,
            resolver_result={"matched": True},
            verified_identifier="doi:10.0000/extraction-job",
            checked_locator="https://example.test/source",
            reviewer_user_id=reviewer.id,
            evidence_checksum="c" * 64,
        )
        unit.session.add(citation)
        unit.flush()
        decision = SourceReviewDecision(
            source_version_id=version.id,
            source_permission_request_id=permission.id,
            sequence_number=1,
            decision=ReviewDecisionKind.APPROVED,
            resulting_status=GovernanceWorkflowStatus.APPROVED,
            reviewer_user_id=reviewer.id,
            actor_role_snapshot=[RoleName.REVIEWER.value],
            reason="Verified citation, access, sensitivity, and extraction scope.",
            citation_verification_evidence_id=citation.id,
            claim_eligibility=ClaimEligibility.ELIGIBLE,
            extraction_permission=ExtractionPermission.SELECTED_SECTIONS,
            storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
            sensitive_information_level=SensitiveInformationLevel.CONTROLLED,
            contradicting_evidence_checked=(
                ContradictionCheckStatus.CHECKED_NONE_FOUND
            ),
            extraction_scope={"sections": ["results"], "locators": ["p. 4"]},
            decision_checksum="d" * 64,
        )
        unit.session.add(decision)
        unit.commit()
    return {
        "operator_id": operator.id,
        "source_version_id": version.id,
        "permission_id": permission.id,
        "decision_id": decision.id,
    }


def _job_kwargs(chain):
    return {
        "source_version_id": chain["source_version_id"],
        "source_review_decision_id": chain["decision_id"],
        "source_permission_request_id": chain["permission_id"],
        "requested_by_user_id": chain["operator_id"],
        "model_name": "glm-4.7",
        "model_snapshot": {"temperature": 0.1, "structured_output": True},
        "prompt_version": "magic-extraction-0.2",
        "prompt_snapshot": {"checksum": "e" * 64, "role_policy": "0.2"},
        "output_schema_version": "extraction-0.2",
        "schema_snapshot": {"checksum": "f" * 64, "strict": True},
        "chunk_config_snapshot": {"size": 1800, "overlap": 200},
    }


def test_job_is_exactly_bound_snapshotted_and_idempotent(
    database, approved_chain
) -> None:
    kwargs = _job_kwargs(approved_chain)
    original_model_snapshot = kwargs["model_snapshot"]
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        repository = ExtractionJobRepository(unit.session)
        first, created = repository.create_job(**kwargs)
        replay, replay_created = repository.create_job(**kwargs)
        assert created is True
        assert replay_created is False
        assert replay.id == first.id
        assert first.source_version_id == approved_chain["source_version_id"]
        assert first.source_review_decision_id == approved_chain["decision_id"]
        assert first.source_permission_request_id == approved_chain["permission_id"]
        assert first.content_hash == hashlib.sha256(
            "A controlled source snapshot for extraction.".encode("utf-8")
        ).hexdigest()
        assert first.scope_snapshot == {
            "sections": ["results"],
            "locators": ["p. 4"],
        }
        assert first.sensitivity == SensitiveInformationLevel.CONTROLLED
        assert first.llm_provider == "GLM"
        assert first.model_snapshot["provider"] == "GLM"
        assert first.model_snapshot["model"] == "glm-4.7"
        assert len(first.configuration_checksum) == 64
        assert len(first.idempotency_key) == 64
        original_model_snapshot["temperature"] = 99
        assert first.model_snapshot["temperature"] == 0.1
        unit.commit()

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ExtractionJob)) == 1


def test_attempt_history_is_append_only_and_supports_safe_retry(
    database, approved_chain
) -> None:
    started = datetime(2026, 8, 9, 1, 0, tzinfo=UTC)
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        repository = ExtractionJobRepository(unit.session)
        job, _ = repository.create_job(**_job_kwargs(approved_chain), now=started)
        first_number = repository.start_attempt(job.id, now=started + timedelta(seconds=1))
        first = repository.fail_attempt(
            job.id,
            attempt_number=first_number,
            error_code="glm_timeout",
            now=started + timedelta(seconds=3),
        )
        assert first.status == ExtractionAttemptStatus.FAILED
        assert first.error_code == "glm_timeout"
        assert first.result_checksum is None

        second_number = repository.start_attempt(
            job.id, now=started + timedelta(seconds=4)
        )
        proposal_payload = {
            "claims": [{"claim_text": "Attention can be directed."}],
            "entities": [{"name": "Misdirection", "type": "technique"}],
            "relationships": [],
        }
        proposal_checksum = canonical_payload_hash(proposal_payload)
        proposal_id = uuid4()
        artifact, artifact_created = repository.store_proposal_artifact(
            job.id,
            proposal_id=proposal_id,
            extraction_run_id="run-test-001",
            schema_version="extraction-0.2",
            payload=proposal_payload,
            payload_checksum=proposal_checksum,
            now=started + timedelta(seconds=5),
        )
        artifact_replay, replay_created = repository.store_proposal_artifact(
            job.id,
            proposal_id=proposal_id,
            extraction_run_id="run-test-001",
            schema_version="extraction-0.2",
            payload=proposal_payload,
            payload_checksum=proposal_checksum,
            now=started + timedelta(seconds=5),
        )
        assert artifact_created is True
        assert replay_created is False
        assert artifact_replay.id == artifact.id
        assert artifact.attempt_number == 2
        second = repository.succeed_attempt(
            job.id,
            attempt_number=second_number,
            result_checksum=proposal_checksum,
            now=started + timedelta(seconds=6),
        )
        replay = repository.succeed_attempt(
            job.id,
            attempt_number=second_number,
            result_checksum=proposal_checksum,
            now=started + timedelta(seconds=7),
        )
        assert replay.id == second.id
        assert job.status == ExtractionJobStatus.SUCCEEDED
        assert job.attempt_count == 2
        assert job.result_checksum == proposal_checksum
        assert repository.get_proposal_artifact(job.id).id == artifact.id
        post_success_replay, post_success_created = (
            repository.store_proposal_artifact(
                job.id,
                proposal_id=proposal_id,
                extraction_run_id="run-test-001",
                schema_version="extraction-0.2",
                payload=proposal_payload,
                payload_checksum=proposal_checksum,
            )
        )
        assert post_success_created is False
        assert post_success_replay.id == artifact.id
        assert [row.attempt_number for row in repository.list_attempts(job.id)] == [
            1,
            2,
        ]
        unit.commit()

    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        attempt = unit.session.scalar(
            select(ExtractionAttempt).where(ExtractionAttempt.id == first.id)
        )
        assert attempt is not None
        attempt.error_code = "another_safe_code"
        with pytest.raises(AppendOnlyMutationError, match="immutable"):
            unit.flush()

    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        artifact = unit.session.scalar(select(ExtractionProposalArtifact))
        assert artifact is not None
        artifact.payload_checksum = "0" * 64
        with pytest.raises(AppendOnlyMutationError, match="immutable"):
            unit.flush()


def test_repository_rejects_raw_errors_secrets_and_invalid_state(
    database, approved_chain
) -> None:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        repository = ExtractionJobRepository(unit.session)
        unsafe = _job_kwargs(approved_chain)
        unsafe["model_snapshot"] = {"api_key": "must-never-persist"}
        with pytest.raises(ExtractionPersistenceError, match="credentials"):
            repository.create_job(**unsafe)

        job, _ = repository.create_job(**_job_kwargs(approved_chain))
        number = repository.start_attempt(job.id)
        with pytest.raises(ExtractionPersistenceError, match="machine code"):
            repository.fail_attempt(
                job.id,
                attempt_number=number,
                error_code="RuntimeError: provider returned secret details",
            )
        with pytest.raises(ExtractionIntegrityError, match="proposal artifact"):
            repository.succeed_attempt(
                job.id,
                attempt_number=number,
                result_checksum="1" * 64,
            )
        with pytest.raises(ExtractionStateError, match="queued or failed"):
            repository.start_attempt(job.id)


def test_job_creation_rejects_incomplete_approval_chain(
    database, approved_chain
) -> None:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        repository = ExtractionJobRepository(unit.session)
        kwargs = _job_kwargs(approved_chain)
        kwargs["source_permission_request_id"] = uuid4()
        with pytest.raises(ExtractionAuthorizationError, match="incomplete"):
            repository.create_job(**kwargs)


def test_content_hash_drift_blocks_job_creation(database, approved_chain) -> None:
    with database.session_factory.begin() as session:
        session.execute(
            update(SourceVersion)
            .where(SourceVersion.id == approved_chain["source_version_id"])
            .values(content_text="tampered content that does not match the hash")
        )

    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        repository = ExtractionJobRepository(unit.session)
        with pytest.raises(ExtractionAuthorizationError, match="integrity"):
            repository.create_job(**_job_kwargs(approved_chain))


def test_proposal_artifact_rejects_source_material_and_requires_active_attempt(
    database, approved_chain
) -> None:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        repository = ExtractionJobRepository(unit.session)
        job, _ = repository.create_job(**_job_kwargs(approved_chain))
        unsafe_payload = {
            "claims": [],
            "candidate": {"content": "verbatim Source text must not persist"},
        }
        with pytest.raises(ExtractionPersistenceError, match="raw chunks"):
            repository.store_proposal_artifact(
                job.id,
                proposal_id=uuid4(),
                extraction_run_id="run-unsafe",
                schema_version="extraction-0.2",
                payload=unsafe_payload,
                payload_checksum=canonical_payload_hash(unsafe_payload),
            )

        safe_payload = {"claims": [], "entities": [], "relationships": []}
        with pytest.raises(ExtractionIntegrityError, match="checksum"):
            repository.store_proposal_artifact(
                job.id,
                proposal_id=uuid4(),
                extraction_run_id="run-bad-checksum",
                schema_version="extraction-0.2",
                payload=safe_payload,
                payload_checksum="0" * 64,
            )
        with pytest.raises(ExtractionStateError, match="active extraction attempt"):
            repository.store_proposal_artifact(
                job.id,
                proposal_id=uuid4(),
                extraction_run_id="run-not-started",
                schema_version="extraction-0.2",
                payload=safe_payload,
                payload_checksum=canonical_payload_hash(safe_payload),
            )


def test_job_specification_cannot_be_rewritten(database, approved_chain) -> None:
    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        repository = ExtractionJobRepository(unit.session)
        job, _ = repository.create_job(**_job_kwargs(approved_chain))
        unit.commit()

    with SqlAlchemyUnitOfWork(database.session_factory) as unit:
        job = unit.session.get(ExtractionJob, job.id)
        assert job is not None
        job.model_name = "glm-rewritten"
        with pytest.raises(AppendOnlyMutationError, match="specification fields"):
            unit.flush()
