from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from knowledge.evidence import (
    ApplicationOrigin,
    ClaimRole,
    EvidenceClass,
    KnowledgeOrigin,
    MagicDomain,
    SourceType,
)
from knowledge.governance import (
    ClaimEligibility,
    ContradictionCheckStatus,
    ExtractionPermission,
    SensitiveInformationLevel,
    StoragePermission,
)
from persistence import Base, DatabaseManager
from persistence.models import (
    ClaimCandidate,
    EvidenceCardVersion,
    ExtractionProposalArtifact,
    Role,
    RoleName,
    SessionRecord,
    SessionStatus,
    SessionTransport,
    User,
    UserRole,
)
from research.models import ContentAccess, DiscoveryProvider, SearchProvenance, SourceCategory
from research.review.workflow_models import (
    CitationMetadataInput,
    CitationVerificationInput,
    CitationVerificationMethod,
    CitationVerificationScope,
    ResolverResult,
    ReviewDecision,
    SourceAccessMetadata,
    SourceReviewCommand,
    SourceVersionCommand,
)
from research.review.workflow_service import (
    GovernanceServiceError,
    ProductionGovernanceService,
)
from research.extraction.production_source import (
    ProductionGLMOutboundPolicy,
    ProductionSourceAdapter,
    ProductionSourceReadinessError,
)
from research.extraction.production_jobs import (
    ExtractionJobCreateCommand,
    ProductionExtractionAuthorizationError,
    ProductionExtractionService,
    ProductionExtractionServiceError,
)
from research.extraction.extractor import ResearchKnowledgeExtractor
from research.extraction.models import ExtractedClaim, ResearchExtractionResult
from app.config import Settings
from security.policy import AuthenticatedActor
from sqlalchemy import func, select


# This fixture exercises the live-session guard in ProductionExtractionService,
# whose clock intentionally uses the real current time.  Keep the fixture's
# session window relative to the test run so the test does not become invalid
# when its formerly fixed expiry date passes.
NOW = datetime.now(UTC).replace(microsecond=0)
SOURCE_TEXT = (
    "Participants may fail to notice an unexpected action while attention is "
    "engaged by the performer's visible gesture."
)


@pytest.fixture
def approved_source(tmp_path):
    database = DatabaseManager(
        f"sqlite+pysqlite:///{tmp_path / 'production-source-adapter.sqlite'}"
    )
    database.start()
    Base.metadata.create_all(database.engine)
    operator_id = uuid4()
    reviewer_id = uuid4()
    operator_session_id = uuid4()
    reviewer_session_id = uuid4()
    with database.session_factory() as session:
        operator_role = Role(name=RoleName.OPERATOR, description="operator")
        reviewer_role = Role(name=RoleName.REVIEWER, description="reviewer")
        operator = User(
            id=operator_id,
            username="OperatorOne",
            username_normalized="operatorone",
            email="operator@example.test",
            email_normalized="operator@example.test",
            password_hash="unused",
        )
        reviewer = User(
            id=reviewer_id,
            username="ReviewerOne",
            username_normalized="reviewerone",
            email="reviewer@example.test",
            email_normalized="reviewer@example.test",
            password_hash="unused",
        )
        session.add_all([operator_role, reviewer_role, operator, reviewer])
        session.flush()
        session.add_all(
            [
                UserRole(user_id=operator_id, role_id=operator_role.id),
                UserRole(user_id=reviewer_id, role_id=reviewer_role.id),
                SessionRecord(
                    id=operator_session_id,
                    user_id=operator_id,
                    token_hash="a" * 64,
                    csrf_token_hash=None,
                    transport=SessionTransport.BEARER,
                    status=SessionStatus.ACTIVE,
                    created_at=NOW,
                    expires_at=NOW + timedelta(hours=1),
                    last_used_at=NOW,
                ),
                SessionRecord(
                    id=reviewer_session_id,
                    user_id=reviewer_id,
                    token_hash="b" * 64,
                    csrf_token_hash=None,
                    transport=SessionTransport.BEARER,
                    status=SessionStatus.ACTIVE,
                    created_at=NOW,
                    expires_at=NOW + timedelta(hours=1),
                    last_used_at=NOW,
                ),
            ]
        )
        session.commit()
    operator_actor = AuthenticatedActor(
        user_id=operator_id,
        username="OperatorOne",
        roles=frozenset({RoleName.OPERATOR}),
        session_id=operator_session_id,
        session_transport=SessionTransport.BEARER,
        session_expires_at=NOW + timedelta(hours=1),
    )
    reviewer_actor = AuthenticatedActor(
        user_id=reviewer_id,
        username="ReviewerOne",
        roles=frozenset({RoleName.REVIEWER}),
        session_id=reviewer_session_id,
        session_transport=SessionTransport.BEARER,
        session_expires_at=NOW + timedelta(hours=1),
    )
    service = ProductionGovernanceService(
        database.session_factory, clock=lambda: NOW
    )
    command = _source_command(SOURCE_TEXT)
    summary = service.register_source(
        operator_actor,
        command,
        idempotency_key="adapter-register-source",
    )
    version = summary.versions[0]
    service.review_source(
        reviewer_actor,
        summary.id,
        SourceReviewCommand(
            source_version_id=version.id,
            source_permission_request_id=version.latest_permission_request_id,
            decision=ReviewDecision.APPROVE,
            reason="Verified the exact public article and extraction scope.",
            claim_eligibility=ClaimEligibility.ELIGIBLE,
            extraction_permission=ExtractionPermission.FULL_TEXT,
            storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
            sensitive_information_level=SensitiveInformationLevel.CONTROLLED,
            contradicting_evidence_checked=ContradictionCheckStatus.NOT_APPLICABLE,
            citation_verification=[
                CitationVerificationInput(
                    scope=CitationVerificationScope.METADATA,
                    method=CitationVerificationMethod.DOI_RESOLVER,
                    resolver_result=ResolverResult.MATCHED,
                    verified_identifier="10.5555/magicforge.adapter",
                    checked_locator="https://doi.org/10.5555/magicforge.adapter",
                    resolver_name="independent DOI metadata review",
                ),
                CitationVerificationInput(
                    scope=CitationVerificationScope.FULL_TEXT,
                    method=CitationVerificationMethod.CONTENT_CHECKSUM,
                    resolver_result=ResolverResult.MATCHED,
                    verified_identifier=version.content_hash,
                    checked_locator="https://example.test/papers/attention-adapter",
                    resolver_name="independent exact-content checksum review",
                )
            ],
        ),
        idempotency_key="adapter-review-source",
    )
    try:
        yield (
            database,
            service,
            operator_actor,
            reviewer_actor,
            summary.id,
            version.id,
        )
    finally:
        database.close()


def _source_command(content: str) -> SourceVersionCommand:
    return SourceVersionCommand(
        citation=CitationMetadataInput(
            title="Attention in a Magic Demonstration",
            authors=["A. Researcher"],
            year=2026,
            venue="Journal of Magic Cognition",
            doi="10.5555/magicforge.adapter",
            url="https://example.test/papers/attention-adapter",
            provenance=[
                SearchProvenance(
                    provider=DiscoveryProvider.EXA,
                    tool_name="web_search_exa",
                    query="attention in magic",
                    rank=1,
                )
            ],
        ),
        source_category=SourceCategory.ACADEMIC,
        source_type=SourceType.JOURNAL_ARTICLE,
        knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        content=content,
        content_access=ContentAccess.WEB_EXTRACT,
        access=SourceAccessMetadata(
            access_method="Exact public article URL",
            permission_notes="Public exact-content review copy.",
            redistribution_allowed=False,
        ),
        requested_extraction_permission=ExtractionPermission.FULL_TEXT,
        requested_storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
        sensitive_information_level=SensitiveInformationLevel.CONTROLLED,
    )


def test_adapter_materializes_only_current_exact_approved_source(approved_source) -> None:
    database, _service, _operator, _reviewer, source_id, version_id = approved_source
    adapter = ProductionSourceAdapter(database.session_factory)

    envelope = adapter.load(version_id)
    listed = adapter.list_eligible()

    assert envelope.source_id == source_id
    assert envelope.source_version_id == version_id
    assert envelope.candidate.content == SOURCE_TEXT
    assert envelope.citation.status.value == "full_text_verified"
    assert envelope.approval.allows_claim_extraction is True
    assert [item.source_version_id for item in listed] == [version_id]
    ProductionGLMOutboundPolicy.require_allowed(envelope, extraction_enabled=True)


def test_adapter_rejects_approved_version_after_newer_version_exists(
    approved_source,
) -> None:
    database, service, operator, _reviewer, source_id, old_version_id = approved_source
    service.add_source_version(
        operator,
        source_id,
        _source_command(SOURCE_TEXT + " A corrected appendix was added."),
        idempotency_key="adapter-new-source-version",
    )

    with pytest.raises(
        ProductionSourceReadinessError, match="newest immutable Source version"
    ):
        ProductionSourceAdapter(database.session_factory).load(old_version_id)


def test_outbound_policy_is_disabled_by_default_and_blocks_secret_material(
    approved_source,
) -> None:
    database, _service, _operator, _reviewer, _source_id, version_id = approved_source
    envelope = ProductionSourceAdapter(database.session_factory).load(version_id)

    with pytest.raises(
        ProductionSourceReadinessError, match="disabled by configuration"
    ):
        ProductionGLMOutboundPolicy.require_allowed(
            envelope, extraction_enabled=False
        )

    restricted = envelope.model_copy(
        update={"sensitivity": SensitiveInformationLevel.SECRET_METHOD}
    )
    with pytest.raises(
        ProductionSourceReadinessError, match="cannot be sent to an external LLM"
    ):
        ProductionGLMOutboundPolicy.require_allowed(
            restricted, extraction_enabled=True
        )


def test_job_service_queues_idempotently_without_sending_source_text(
    approved_source,
) -> None:
    database, _governance, operator, _reviewer, _source_id, version_id = approved_source
    service = ProductionExtractionService(
        database.session_factory,
        Settings(glm_model="glm-4.7", chunk_size=1800, chunk_overlap=200),
    )

    first = service.queue(
        operator,
        ExtractionJobCreateCommand(source_version_id=version_id),
        request_id="queue-request-1",
    )
    replay = service.queue(
        operator,
        ExtractionJobCreateCommand(source_version_id=version_id),
        request_id="queue-request-2",
    )

    assert first.created is True
    assert replay.created is False
    assert replay.job.id == first.job.id
    assert first.job.status.value == "queued"
    assert first.job.provider == "GLM"
    assert first.job.model == "glm-4.7"
    assert first.job.attempts == ()
    assert service.get_job(operator, first.job.id) == first.job
    assert [item.id for item in service.list_jobs(operator)] == [first.job.id]


def test_job_service_uses_live_rbac_and_rejects_reviewer(
    approved_source,
) -> None:
    database, _governance, _operator, reviewer, _source_id, version_id = approved_source
    service = ProductionExtractionService(database.session_factory, Settings())

    with pytest.raises(
        ProductionExtractionAuthorizationError, match="live operator session"
    ):
        service.queue(
            reviewer,
            ExtractionJobCreateCommand(source_version_id=version_id),
        )


class _FakeExtractionGLM:
    model = "glm-4.7"

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def generate_structured(self, prompt, _response_model, **_kwargs):
        self._calls.append(prompt)
        return ResearchExtractionResult(
            magic_category="misdirection",
            claims=[
                ExtractedClaim(
                    statement=SOURCE_TEXT,
                    evidence_excerpt=SOURCE_TEXT,
                    locator=(
                        "https://example.test/papers/attention-adapter#results"
                    ),
                    confidence=0.91,
                    claim_role=ClaimRole.RESULT,
                    evidence_class=EvidenceClass.CONTROLLED_EXPERIMENT,
                    applicable_domains=[MagicDomain.THEORY],
                    ontology_paths=[
                        "psychology.attention.inattentional_blindness"
                    ],
                    topic_tags=["attention"],
                    magic_application=(
                        "Direct visible attention before a covert action."
                    ),
                    application_origin=ApplicationOrigin.SOURCE_STATED,
                    limitations=["The exact viewing context may not generalize."],
                    population_context="Spectators in a controlled demonstration.",
                    performance_context="A visible gesture during performance.",
                )
            ],
        )


def _fake_extractor_factory(calls: list[str]):
    def factory(snapshot, _settings):
        return ResearchKnowledgeExtractor(
            _FakeExtractionGLM(calls),
            chunk_size=snapshot.chunk_size,
            chunk_overlap=snapshot.chunk_overlap,
        )

    return factory


def test_job_run_persists_sanitized_artifact_and_pending_claim_only(
    approved_source,
) -> None:
    database, _governance, operator, _reviewer, _source_id, version_id = approved_source
    calls: list[str] = []
    service = ProductionExtractionService(
        database.session_factory,
        Settings(
            glm_model="glm-4.7",
            production_glm_extraction_enabled=True,
            chunk_size=1800,
            chunk_overlap=200,
        ),
        extractor_factory=_fake_extractor_factory(calls),
    )
    queued = service.queue(
        operator,
        ExtractionJobCreateCommand(source_version_id=version_id),
    )

    result = service.run(operator, queued.job.id)
    replay = service.run(operator, queued.job.id)

    assert result.job.status.value == "succeeded"
    assert result.job.attempt_count == 1
    assert result.claim_candidates == 1
    assert result.entity_proposals == 0
    assert result.relationship_proposals == 0
    assert result.artifact_reused is False
    assert replay.proposal_checksum == result.proposal_checksum
    assert replay.artifact_reused is True
    assert len(calls) == 1

    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ClaimCandidate)) == 1
        assert (
            session.scalar(select(func.count()).select_from(EvidenceCardVersion))
            == 0
        )
        artifact = session.scalar(select(ExtractionProposalArtifact))
        assert artifact is not None
        assert "candidate" not in artifact.payload
        assert "content" not in artifact.payload
        assert "raw_chunks" not in artifact.payload
        assert artifact.payload["claim_submissions"][0]["command"][
            "extraction_provenance"
        ]["llm_provider"] == "GLM"


def test_job_run_switch_is_fail_closed_before_attempt(approved_source) -> None:
    database, _governance, operator, _reviewer, _source_id, version_id = approved_source
    calls: list[str] = []
    service = ProductionExtractionService(
        database.session_factory,
        Settings(production_glm_extraction_enabled=False),
        extractor_factory=_fake_extractor_factory(calls),
    )
    queued = service.queue(
        operator,
        ExtractionJobCreateCommand(source_version_id=version_id),
    )

    with pytest.raises(
        ProductionExtractionServiceError,
        match="disabled by configuration",
    ):
        service.run(operator, queued.job.id)

    unchanged = service.get_job(operator, queued.job.id)
    assert unchanged.status.value == "queued"
    assert unchanged.attempt_count == 0
    assert calls == []


def test_failed_claim_submission_reuses_artifact_without_second_glm_call(
    approved_source,
) -> None:
    database, _governance, operator, _reviewer, _source_id, version_id = approved_source
    calls: list[str] = []

    class ToggleGovernance:
        fail = True
        submissions = 0

        def submit_claim(self, *_args, **_kwargs):
            self.submissions += 1
            if self.fail:
                raise GovernanceServiceError(
                    "test_claim_failure", "private provider diagnostic"
                )
            return object()

    toggle = ToggleGovernance()
    service = ProductionExtractionService(
        database.session_factory,
        Settings(
            production_glm_extraction_enabled=True,
            chunk_size=1800,
            chunk_overlap=200,
        ),
        extractor_factory=_fake_extractor_factory(calls),
        governance_factory=lambda _factory: toggle,
    )
    queued = service.queue(
        operator,
        ExtractionJobCreateCommand(source_version_id=version_id),
    )

    with pytest.raises(ProductionExtractionServiceError) as failure:
        service.run(operator, queued.job.id)
    assert failure.value.code == "claim_submission_rejected"
    failed = service.get_job(operator, queued.job.id)
    assert failed.status.value == "failed"
    assert failed.error_code == "claim_submission_rejected"
    assert "private provider diagnostic" not in str(failure.value)

    toggle.fail = False
    completed = service.run(operator, queued.job.id)
    assert completed.job.status.value == "succeeded"
    assert completed.job.attempt_count == 2
    assert completed.artifact_reused is True
    assert len(calls) == 1
