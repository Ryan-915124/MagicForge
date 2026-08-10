"""Strict service-layer tests for the Checkpoint 3 governance workflow.

These tests intentionally use real database-backed authenticated sessions.  They
exercise the service boundary directly so that HTTP validation cannot hide a
transaction, authorization, or append-only persistence regression.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update

from knowledge.evidence import (
    ClaimRole,
    ConfidenceAssessment,
    ConfidenceDimension,
    ContradictionStatus,
    EvidenceCard,
    EvidenceClass,
    EvidenceLocator,
    KnowledgeOrigin,
    MagicDomain,
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
from persistence import (
    AppendOnlyMutationError,
    AuditEvent,
    Base,
    ClaimCandidate,
    ClaimReviewDecision,
    DatabaseManager,
    EvidenceCardVersion,
    GovernanceWorkflowStatus,
    IdempotencyRecord,
    ReviewDecisionKind,
    Role,
    RoleName,
    SessionTransport,
    SourceReviewDecision,
    SourceVersion,
    User,
    UserRole,
)
from persistence.idempotency import canonical_payload_hash
from persistence.models import SourcePermissionRequest
from research.citation.models import PeerReviewStatus
from research.models import (
    ContentAccess,
    DiscoveryProvider,
    SearchProvenance,
    SourceCategory,
)
from research.review.workflow_models import (
    CitationMetadataInput,
    CitationVerificationInput,
    CitationVerificationMethod,
    CitationVerificationScope,
    ClaimCandidateCommand,
    ClaimReviewCommand,
    ExtractionProducer,
    ExtractionProvenanceInput,
    ResolverResult,
    ReviewDecision,
    SourceAccessMetadata,
    SourcePermissionRequestCommand,
    SourceReviewCommand,
    SourceVersionCommand,
    WorkflowStatus,
)
from research.review.workflow_service import (
    GovernanceAuthorizationError,
    GovernanceConflictError,
    GovernanceInputError,
    GovernanceNotFoundError,
    ProductionGovernanceService,
)
from research.review.source_permission_policy import (
    permission_request_checksum_payload,
)
from security.policy import AuthenticatedActor, activate_break_glass
from security.services import AdminService, AuthService


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
CLAIM_TEXT = (
    "Participants may fail to notice an unexpected action during the "
    "demonstration."
)
SECOND_CLAIM_TEXT = (
    "Participants may reconstruct the sequence after the demonstration."
)
SOURCE_TEXT = f"{CLAIM_TEXT} {SECOND_CLAIM_TEXT}"


class FastPasswordHasher:
    """Cheap password adapter suitable only for isolated service tests."""

    MIN_LENGTH = 12
    MAX_LENGTH = 128

    def validate_new_password(self, password, *, identity_terms=()):
        if len(password) < self.MIN_LENGTH:
            raise ValueError("password too short")
        if password.casefold() in {
            value.casefold() for value in identity_terms if value
        }:
            raise ValueError("password equals identity")

    def hash(self, password):
        return "test$" + hashlib.sha256(password.encode()).hexdigest()

    def verify(self, password_hash, password):
        return hmac.compare_digest(password_hash, self.hash(password))

    def needs_rehash(self, password_hash):
        return False


@dataclass(frozen=True)
class GovernanceHarness:
    database: DatabaseManager
    service: ProductionGovernanceService
    operator: AuthenticatedActor
    reviewer: AuthenticatedActor
    reader: AuthenticatedActor
    dual_role: AuthenticatedActor


@pytest.fixture
def governance(tmp_path):
    database = DatabaseManager(
        f"sqlite+pysqlite:///{tmp_path / 'governance-workflow.sqlite'}"
    )
    database.start()
    Base.metadata.create_all(database.engine)
    with database.session_factory() as session:
        session.add_all(
            [Role(name=role_name, description=role_name.value) for role_name in RoleName]
        )
        session.commit()

    passwords = {
        "admin": "a carefully chosen admin passphrase",
        "operator": "a carefully chosen operator passphrase",
        "reviewer": "a carefully chosen reviewer passphrase",
        "reader": "a carefully chosen reader passphrase",
        "dual": "a carefully chosen dual role passphrase",
    }
    hasher = FastPasswordHasher()
    admin_service = AdminService(
        database.session_factory,
        password_hasher=hasher,
        clock=lambda: NOW,
    )
    admin_service.bootstrap_initial_admin(
        username="ArchiveAdmin",
        email="admin@example.test",
        password=passwords["admin"],
    )
    auth_service = AuthService(
        database.session_factory,
        password_hasher=hasher,
        clock=lambda: NOW,
    )
    admin = auth_service.login(
        identifier="ArchiveAdmin",
        password=passwords["admin"],
        transport=SessionTransport.BEARER,
    )
    users = (
        ("OperatorOne", "operator", [RoleName.OPERATOR]),
        ("ReviewerOne", "reviewer", [RoleName.REVIEWER]),
        ("ReaderOne", "reader", [RoleName.READER]),
        ("DualRoleOne", "dual", [RoleName.OPERATOR, RoleName.REVIEWER]),
    )
    for username, password_key, roles in users:
        admin_service.create_user(
            admin.actor,
            username=username,
            email=f"{password_key}@example.test",
            password=passwords[password_key],
            roles=roles,
            reason=f"Provision {username} for an isolated governance test.",
        )

    actors: dict[str, AuthenticatedActor] = {}
    for username, password_key, _roles in users:
        actors[password_key] = auth_service.login(
            identifier=username,
            password=passwords[password_key],
            transport=SessionTransport.BEARER,
        ).actor

    try:
        yield GovernanceHarness(
            database=database,
            service=ProductionGovernanceService(
                database.session_factory, clock=lambda: NOW
            ),
            operator=actors["operator"],
            reviewer=actors["reviewer"],
            reader=actors["reader"],
            dual_role=actors["dual"],
        )
    finally:
        database.close()


def _source_command(
    *,
    slug: str = "attention",
    content: str = SOURCE_TEXT,
    verification: str = "full_text",
    storage_permission: StoragePermission = StoragePermission.DERIVED_WITH_SHORT_EXCERPT,
    sensitivity: SensitiveInformationLevel = SensitiveInformationLevel.CONTROLLED,
    extraction_permission: ExtractionPermission = ExtractionPermission.FULL_TEXT,
    requested_scope_locators: list[str] | None = None,
) -> SourceVersionCommand:
    doi = f"10.5555/magicforge.workflow.{slug}"
    url = f"https://example.test/papers/{slug}"
    checks: list[CitationVerificationInput] = []
    if verification in {"metadata", "full_text", "mismatch"}:
        checks.append(
            CitationVerificationInput(
                scope=CitationVerificationScope.METADATA,
                method=CitationVerificationMethod.DOI_RESOLVER,
                resolver_result=(
                    ResolverResult.MISMATCH
                    if verification == "mismatch"
                    else ResolverResult.MATCHED
                ),
                verified_identifier=doi,
                checked_locator=f"https://doi.org/{doi}",
                resolver_name="test-doi-resolver",
            )
        )
    if verification == "full_text":
        checks.append(
            CitationVerificationInput(
                scope=CitationVerificationScope.FULL_TEXT,
                method=CitationVerificationMethod.CONTENT_CHECKSUM,
                resolver_result=ResolverResult.MATCHED,
                verified_identifier=hashlib.sha256(content.encode()).hexdigest(),
                checked_locator=url,
                resolver_name="test-content-checksum",
            )
        )
    return SourceVersionCommand(
        citation=CitationMetadataInput(
            title=f"Attention study {slug}",
            authors=["A. Researcher"],
            year=2024,
            venue="Journal of Test Cognition",
            doi=doi,
            url=url,
            peer_review_status=PeerReviewStatus.PEER_REVIEWED,
            provenance=[
                SearchProvenance(
                    provider=DiscoveryProvider.EXA,
                    tool_name="test-discovery",
                    query=f"attention magic {slug}",
                    rank=1,
                    retrieved_at=NOW,
                )
            ],
        ),
        source_category=SourceCategory.ACADEMIC,
        source_type=SourceType.JOURNAL_ARTICLE,
        knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        content=content,
        content_access=ContentAccess.FULL_TEXT,
        access=SourceAccessMetadata(
            access_method="publisher full text",
            license_name="test-only",
            redistribution_allowed=True,
        ),
        requested_extraction_permission=extraction_permission,
        requested_storage_permission=storage_permission,
        requested_scope_locators=requested_scope_locators or [],
        sensitive_information_level=sensitivity,
        citation_verification=checks,
    )


def _source_review_command(
    version_id,
    *,
    source_permission_request_id,
    decision: ReviewDecision = ReviewDecision.APPROVE,
    storage_permission: StoragePermission = StoragePermission.DERIVED_WITH_SHORT_EXCERPT,
    sensitivity: SensitiveInformationLevel = SensitiveInformationLevel.CONTROLLED,
    citation_verification: list[CitationVerificationInput] | None = None,
    extraction_permission: ExtractionPermission = ExtractionPermission.FULL_TEXT,
    extraction_scope_locators: list[str] | None = None,
) -> SourceReviewCommand:
    return SourceReviewCommand(
        source_version_id=version_id,
        source_permission_request_id=source_permission_request_id,
        decision=decision,
        reason="Citation and immutable source content were independently checked.",
        claim_eligibility=(
            ClaimEligibility.ELIGIBLE
            if decision == ReviewDecision.APPROVE
            else ClaimEligibility.INELIGIBLE
        ),
        extraction_permission=(
            extraction_permission
            if decision == ReviewDecision.APPROVE
            else ExtractionPermission.NONE
        ),
        storage_permission=(
            storage_permission
            if decision == ReviewDecision.APPROVE
            else StoragePermission.NONE
        ),
        sensitive_information_level=sensitivity,
        contradicting_evidence_checked=ContradictionCheckStatus.NOT_APPLICABLE,
        citation_verification=citation_verification or [],
        extraction_scope_locators=extraction_scope_locators or [],
    )


def _permission_request_command(
    version_id,
    *,
    extraction_permission: ExtractionPermission = ExtractionPermission.FULL_TEXT,
    storage_permission: StoragePermission = StoragePermission.DERIVED_KNOWLEDGE_ONLY,
    scope: list[str] | None = None,
    rights_basis: str = "Rights evidence was re-checked by the operator.",
    rights_evidence: list[str] | None = None,
    reason: str = "Amend the Source review ceiling with updated rights evidence.",
) -> SourcePermissionRequestCommand:
    return SourcePermissionRequestCommand(
        source_version_id=version_id,
        requested_extraction_permission=extraction_permission,
        requested_storage_permission=storage_permission,
        requested_scope_locators=scope or [],
        rights_basis=rights_basis,
        rights_evidence=rights_evidence or ["https://example.test/rights-record"],
        reason=reason,
    )


def _register_and_approve_source(
    governance: GovernanceHarness,
    *,
    slug: str,
    content: str = SOURCE_TEXT,
    storage_permission: StoragePermission = StoragePermission.DERIVED_WITH_SHORT_EXCERPT,
    sensitivity: SensitiveInformationLevel = SensitiveInformationLevel.CONTROLLED,
):
    source_command = _source_command(
        slug=slug,
        content=content,
        storage_permission=storage_permission,
        sensitivity=sensitivity,
    )
    source = governance.service.register_source(
        governance.operator,
        source_command,
        idempotency_key=f"register-{slug}",
    )
    version_id = source.versions[0].id
    permission_request_id = source.versions[0].latest_permission_request_id
    assert permission_request_id is not None
    governance.service.review_source(
        governance.reviewer,
        source.id,
        _source_review_command(
            version_id,
            source_permission_request_id=permission_request_id,
            storage_permission=storage_permission,
            sensitivity=sensitivity,
            citation_verification=source_command.citation_verification,
        ),
        idempotency_key=f"approve-source-{slug}",
    )
    return source.id, version_id


def _claim_command(
    source_version_id,
    *,
    claim: str = CLAIM_TEXT,
    role: ClaimRole = ClaimRole.RESULT,
) -> ClaimCandidateCommand:
    return ClaimCandidateCommand(
        source_version_id=source_version_id,
        claim=claim,
        claim_role=role,
        proposed_evidence_class=EvidenceClass.CONTROLLED_EXPERIMENT,
        applicable_domain=[MagicDomain.THEORY],
        ontology_paths=["psychology.attention.inattentional_blindness"],
        topic_tags=["attention", "misdirection"],
        locator=EvidenceLocator(
            media_type="web",
            source_locator="https://example.test/papers/claim-locator",
        ),
        evidence_excerpt=claim,
        proposed_limitations=["The observation comes from one experimental context."],
        population_context="Participants in a controlled attention experiment.",
        performance_context="A demonstration containing an unexpected action.",
        extraction_confidence=0.9,
        extraction_provenance=ExtractionProvenanceInput(
            producer=ExtractionProducer.PIPELINE,
            extractor="governance-service-test",
            extraction_schema_version="extraction-0.2",
            run_id="checkpoint-3-test",
            tool_version="1.0",
        ),
    )


def _confidence(reviewer: str) -> ConfidenceAssessment:
    return ConfidenceAssessment(
        provenance_quality=ConfidenceDimension(
            score=1.0, reason="The immutable source version is directly linked."
        ),
        method_rigor=ConfidenceDimension(
            score=1.0, reason="The source reports a controlled experiment."
        ),
        claim_directness=ConfidenceDimension(
            score=1.0, reason="The excerpt directly states the claim."
        ),
        consistency=ConfidenceDimension(
            score=1.0, reason="The contradiction check found no conflict."
        ),
        magic_applicability=ConfidenceDimension(
            score=1.0, reason="The finding applies to attention management."
        ),
        assessed_by=reviewer,
        assessed_at=NOW,
    )


def _claim_review_command(
    reviewer: str,
    *,
    decision: ReviewDecision = ReviewDecision.APPROVE,
    storage_permission: StoragePermission = StoragePermission.DERIVED_WITH_SHORT_EXCERPT,
    sensitivity: SensitiveInformationLevel = SensitiveInformationLevel.CONTROLLED,
) -> ClaimReviewCommand:
    if decision == ReviewDecision.REJECT:
        return ClaimReviewCommand(
            decision=decision,
            reason="The claim is not eligible for evidence projection.",
            claim_eligibility=ClaimEligibility.INELIGIBLE,
            storage_permission=StoragePermission.NONE,
            sensitive_information_level=sensitivity,
            secret_exposure_level=SecretExposureLevel.GENERAL_PRINCIPLE,
        )
    return ClaimReviewCommand(
        decision=decision,
        reason="The atomic claim is directly supported by its source locator.",
        confidence=_confidence(reviewer),
        claim_eligibility=ClaimEligibility.ELIGIBLE,
        storage_permission=storage_permission,
        sensitive_information_level=sensitivity,
        contradiction_status=ContradictionStatus.NONE_FOUND,
        contradicting_evidence_checked=(
            ContradictionCheckStatus.CHECKED_NONE_FOUND
        ),
        limitations=["The observation comes from one experimental context."],
        secret_exposure_level=SecretExposureLevel.GENERAL_PRINCIPLE,
    )


def _submit_claim(
    governance: GovernanceHarness,
    version_id,
    *,
    key: str,
    actor: AuthenticatedActor | None = None,
    role: ClaimRole = ClaimRole.RESULT,
    claim: str = CLAIM_TEXT,
):
    return governance.service.submit_claim(
        actor or governance.operator,
        _claim_command(version_id, claim=claim, role=role),
        idempotency_key=key,
    )


def _count(session, model, *criteria) -> int:
    statement = select(func.count()).select_from(model)
    if criteria:
        statement = statement.where(*criteria)
    return int(session.scalar(statement) or 0)


def _permission_request_id(governance: GovernanceHarness, version_id):
    with governance.database.session_factory() as session:
        request_id = session.scalar(
            select(SourcePermissionRequest.id)
            .where(SourcePermissionRequest.source_version_id == version_id)
            .order_by(SourcePermissionRequest.sequence_number.desc())
            .limit(1)
        )
    assert request_id is not None
    return request_id


def _break_glass_reviewer(
    governance: GovernanceHarness,
) -> tuple[AuthenticatedActor, AuthenticatedActor]:
    """Give the existing admin an independent Reviewer role for one test DB."""

    hasher = FastPasswordHasher()
    auth_service = AuthService(
        governance.database.session_factory,
        password_hasher=hasher,
        clock=lambda: NOW,
    )
    admin_service = AdminService(
        governance.database.session_factory,
        password_hasher=hasher,
        clock=lambda: NOW,
    )
    normal = auth_service.login(
        identifier="ArchiveAdmin",
        password="a carefully chosen admin passphrase",
        transport=SessionTransport.BEARER,
    ).actor
    assert normal.user_id is not None
    admin_service.grant_role(
        normal,
        user_id=normal.user_id,
        role=RoleName.REVIEWER,
        reason="Grant independent review authority for restricted governance testing.",
    )
    elevated = activate_break_glass(
        normal,
        reason="Review a restricted governance classification safely.",
    )
    return normal, elevated


def _terminal_claim_decision(
    candidate: ClaimCandidate,
    *,
    reviewer_user_id,
    sequence_number: int,
    status: GovernanceWorkflowStatus,
    supersedes_decision_id=None,
) -> ClaimReviewDecision:
    decision = {
        GovernanceWorkflowStatus.REJECTED: ReviewDecisionKind.REJECTED,
        GovernanceWorkflowStatus.REVOKED: ReviewDecisionKind.REVOKED,
    }[status]
    checksum = hashlib.sha256(
        f"{candidate.id}:{sequence_number}:{status.value}".encode()
    ).hexdigest()
    return ClaimReviewDecision(
        id=uuid4(),
        claim_candidate_id=candidate.id,
        sequence_number=sequence_number,
        decision=decision,
        resulting_status=status,
        reviewer_user_id=reviewer_user_id,
        actor_role_snapshot=[RoleName.REVIEWER.value],
        reason=f"Persist the {status.value} decision for guard verification.",
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
        decision_checksum=checksum,
        supersedes_decision_id=supersedes_decision_id,
        created_at=NOW + timedelta(seconds=sequence_number),
    )


def test_source_versions_are_immutable_and_duplicate_hash_is_rejected(
    governance,
) -> None:
    first_command = _source_command(slug="immutable", content="First immutable text.")
    registered = governance.service.register_source(
        governance.operator,
        first_command,
        idempotency_key="source-immutable-v1",
    )
    first_id = registered.versions[0].id
    second_command = _source_command(
        slug="immutable", content="Second immutable text."
    )
    updated = governance.service.add_source_version(
        governance.operator,
        registered.id,
        second_command,
        idempotency_key="source-immutable-v2",
    )

    assert [version.version for version in updated.versions] == [1, 2]
    assert updated.versions[0].content_hash == hashlib.sha256(
        b"First immutable text."
    ).hexdigest()
    assert updated.versions[1].content_hash == hashlib.sha256(
        b"Second immutable text."
    ).hexdigest()

    with governance.database.session_factory() as session:
        first = session.get(SourceVersion, first_id)
        second = session.get(SourceVersion, updated.versions[1].id)
        assert first is not None and first.content_text == "First immutable text."
        assert second is not None and second.supersedes_version_id == first.id

    with pytest.raises(GovernanceConflictError) as duplicate:
        governance.service.add_source_version(
            governance.operator,
            registered.id,
            second_command,
            idempotency_key="source-immutable-duplicate",
        )
    assert duplicate.value.code == "source_content_already_versioned"
    with governance.database.session_factory() as session:
        assert _count(session, SourceVersion) == 2


def test_permission_requests_are_append_only_idempotent_and_non_projecting(
    governance,
) -> None:
    source_command = _source_command(slug="permission-amendment")
    source = governance.service.register_source(
        governance.operator,
        source_command,
        idempotency_key="permission-amendment-source",
    )
    version = source.versions[0]
    initial_request_id = version.latest_permission_request_id
    assert initial_request_id is not None
    command = _permission_request_command(version.id)

    with pytest.raises(GovernanceAuthorizationError):
        governance.service.add_source_permission_request(
            governance.reviewer,
            source.id,
            version.id,
            command,
            idempotency_key="reviewer-permission-forbidden",
        )

    amended = governance.service.add_source_permission_request(
        governance.operator,
        source.id,
        version.id,
        command,
        idempotency_key="test-key",
    )
    assert amended.sequence == 2
    assert amended.supersedes_request_id == initial_request_id
    assert governance.service.add_source_permission_request(
        governance.operator,
        source.id,
        version.id,
        command,
        idempotency_key="test-key",
    ) == amended

    same_material = _permission_request_command(
        version.id,
        reason="A changed audit reason alone is not a permission amendment.",
    )
    with pytest.raises(GovernanceConflictError) as unchanged:
        governance.service.add_source_permission_request(
            governance.operator,
            source.id,
            version.id,
            same_material,
            idempotency_key="permission-amendment-unchanged",
        )
    assert unchanged.value.code == "permission_request_unchanged"

    changed = _permission_request_command(
        version.id,
        rights_evidence=["https://example.test/different-rights-record"],
    )
    with pytest.raises(GovernanceConflictError) as idempotency_conflict:
        governance.service.add_source_permission_request(
            governance.operator,
            source.id,
            version.id,
            changed,
            idempotency_key="test-key",
        )
    assert idempotency_conflict.value.code == "idempotency_conflict"

    detail = governance.service.get_source(governance.reviewer, source.id)
    assert detail.versions[0].latest_permission_request_id == amended.id
    assert [item.sequence for item in detail.versions[0].permission_requests] == [
        1,
        2,
    ]
    queue = governance.service.list_sources(
        governance.reviewer, status=WorkflowStatus.SUBMITTED
    )
    queue_item = next(item for item in queue if item.source_version_id == version.id)
    assert queue_item.latest_permission_request_id == amended.id
    with governance.database.session_factory() as session:
        assert _count(
            session,
            SourcePermissionRequest,
            SourcePermissionRequest.source_version_id == version.id,
        ) == 2
        assert _count(
            session,
            SourceReviewDecision,
            SourceReviewDecision.source_version_id == version.id,
        ) == 0
        assert _count(
            session,
            ClaimCandidate,
            ClaimCandidate.source_version_id == version.id,
        ) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "requested_extraction_permission": ExtractionPermission.SELECTED_SECTIONS,
            "requested_scope_locators": [],
        },
        {
            "requested_extraction_permission": ExtractionPermission.FULL_TEXT,
            "requested_scope_locators": ["Results"],
        },
        {
            "requested_extraction_permission": ExtractionPermission.METADATA_ONLY,
            "requested_storage_permission": StoragePermission.DERIVED_KNOWLEDGE_ONLY,
        },
        {"rights_basis": "   "},
        {"rights_evidence": ["   "]},
        {"reason": "   "},
    ],
)
def test_permission_request_command_rejects_invalid_rights_shapes(overrides) -> None:
    payload = _permission_request_command(uuid4()).model_dump(mode="python")
    payload.update(overrides)
    with pytest.raises(ValidationError):
        SourcePermissionRequestCommand.model_validate(payload)


def test_permission_request_binding_is_latest_cross_version_and_independent(
    governance,
) -> None:
    source_command = _source_command(slug="permission-binding")
    source = governance.service.register_source(
        governance.operator,
        source_command,
        idempotency_key="permission-binding-source",
    )
    version = source.versions[0]
    initial_request_id = version.latest_permission_request_id
    assert initial_request_id is not None
    selected = _permission_request_command(
        version.id,
        extraction_permission=ExtractionPermission.SELECTED_SECTIONS,
        storage_permission=StoragePermission.NONE,
        scope=["Results"],
    )
    latest = governance.service.add_source_permission_request(
        governance.dual_role,
        source.id,
        version.id,
        selected,
        idempotency_key="permission-binding-selected",
    )

    with pytest.raises(GovernanceConflictError) as stale:
        governance.service.review_source(
            governance.reviewer,
            source.id,
            _source_review_command(
                version.id,
                source_permission_request_id=initial_request_id,
                citation_verification=source_command.citation_verification,
            ),
            idempotency_key="permission-binding-stale-review",
        )
    assert stale.value.code == "stale_source_permission_request"

    with pytest.raises(GovernanceConflictError) as self_review:
        governance.service.review_source(
            governance.dual_role,
            source.id,
            _source_review_command(
                version.id,
                source_permission_request_id=latest.id,
                extraction_permission=ExtractionPermission.SELECTED_SECTIONS,
                extraction_scope_locators=["Results"],
                storage_permission=StoragePermission.NONE,
                citation_verification=source_command.citation_verification,
            ),
            idempotency_key="permission-binding-self-review",
        )
    assert self_review.value.code == "permission_request_self_review_forbidden"

    with pytest.raises(GovernanceConflictError) as ceiling:
        governance.service.review_source(
            governance.reviewer,
            source.id,
            _source_review_command(
                version.id,
                source_permission_request_id=latest.id,
                citation_verification=source_command.citation_verification,
            ),
            idempotency_key="permission-binding-ceiling",
        )
    assert ceiling.value.code == "extraction_permission_exceeded"

    second_command = _source_command(
        slug="permission-binding", content="A distinct immutable second version."
    )
    second = governance.service.add_source_version(
        governance.operator,
        source.id,
        second_command,
        idempotency_key="permission-binding-source-v2",
    )
    second_version = second.versions[-1]
    with pytest.raises(GovernanceConflictError) as cross_version:
        governance.service.add_source_permission_request(
            governance.operator,
            source.id,
            second_version.id,
            _permission_request_command(version.id),
            idempotency_key="permission-binding-cross-version",
        )
    assert cross_version.value.code == "permission_request_version_mismatch"

    approved = governance.service.review_source(
        governance.reviewer,
        source.id,
        _source_review_command(
            version.id,
            source_permission_request_id=latest.id,
            extraction_permission=ExtractionPermission.SELECTED_SECTIONS,
            extraction_scope_locators=["Results"],
            storage_permission=StoragePermission.NONE,
            citation_verification=source_command.citation_verification,
        ),
        idempotency_key="permission-binding-approved",
    )
    assert approved.versions[0].status == WorkflowStatus.APPROVED
    with governance.database.session_factory() as session:
        decision = session.scalar(
            select(SourceReviewDecision).where(
                SourceReviewDecision.source_version_id == version.id
            )
        )
        assert decision is not None
        assert decision.source_permission_request_id == latest.id

    with pytest.raises(GovernanceConflictError) as terminal:
        governance.service.add_source_permission_request(
            governance.operator,
            source.id,
            version.id,
            _permission_request_command(
                version.id,
                rights_evidence=["https://example.test/post-review-rights"],
            ),
            idempotency_key="permission-binding-after-review",
        )
    assert terminal.value.code == "source_not_submitted"


def test_full_text_request_allows_reviewer_to_narrow_to_selected_scope(
    governance,
) -> None:
    source_command = _source_command(slug="permission-full-to-selected")
    source = governance.service.register_source(
        governance.operator,
        source_command,
        idempotency_key="permission-full-to-selected-source",
    )
    version = source.versions[0]
    request_id = version.latest_permission_request_id
    assert request_id is not None

    reviewed = governance.service.review_source(
        governance.reviewer,
        source.id,
        _source_review_command(
            version.id,
            source_permission_request_id=request_id,
            extraction_permission=ExtractionPermission.SELECTED_SECTIONS,
            extraction_scope_locators=["Results", "Appendix A"],
            citation_verification=source_command.citation_verification,
        ),
        idempotency_key="permission-full-to-selected-review",
    )
    assert reviewed.versions[0].status == WorkflowStatus.APPROVED
    with governance.database.session_factory() as session:
        decision = governance.service._approved_source_decision(
            session,
            version.id,
        )
        assert decision.extraction_permission == ExtractionPermission.SELECTED_SECTIONS
        assert decision.extraction_scope["locators"] == ["Results", "Appendix A"]


def test_reviewer_cannot_have_submitted_any_request_in_permission_history(
    governance,
) -> None:
    source_command = _source_command(slug="permission-history-independence")
    source = governance.service.register_source(
        governance.operator,
        source_command,
        idempotency_key="permission-history-independence-source",
    )
    version = source.versions[0]
    governance.service.add_source_permission_request(
        governance.dual_role,
        source.id,
        version.id,
        _permission_request_command(
            version.id,
            rights_evidence=["https://example.test/rights/dual-role"],
        ),
        idempotency_key="permission-history-independence-dual",
    )
    latest = governance.service.add_source_permission_request(
        governance.operator,
        source.id,
        version.id,
        _permission_request_command(
            version.id,
            rights_evidence=["https://example.test/rights/operator-final"],
        ),
        idempotency_key="permission-history-independence-operator",
    )

    with pytest.raises(GovernanceConflictError) as self_review:
        governance.service.review_source(
            governance.dual_role,
            source.id,
            _source_review_command(
                version.id,
                source_permission_request_id=latest.id,
                storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
                citation_verification=source_command.citation_verification,
            ),
            idempotency_key="permission-history-independence-review",
        )
    assert self_review.value.code == "permission_request_self_review_forbidden"


def test_permission_request_history_must_be_a_contiguous_chain(governance) -> None:
    source = governance.service.register_source(
        governance.operator,
        _source_command(slug="permission-chain"),
        idempotency_key="permission-chain-source",
    )
    version = source.versions[0]
    first_id = version.latest_permission_request_id
    assert first_id is not None
    second = governance.service.add_source_permission_request(
        governance.operator,
        source.id,
        version.id,
        _permission_request_command(
            version.id,
            rights_evidence=["https://example.test/rights/chain-second"],
        ),
        idempotency_key="permission-chain-second",
    )
    assert second.supersedes_request_id == first_id

    with governance.database.session_factory() as session:
        payload = permission_request_checksum_payload(
            source_version_id=version.id,
            sequence_number=3,
            requested_extraction_permission=ExtractionPermission.FULL_TEXT,
            requested_storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
            requested_scope_locators=[],
            rights_basis="A syntactically valid but incorrectly linked request.",
            rights_evidence=["https://example.test/rights/chain-third"],
            reason="Exercise the complete supersession-chain integrity gate.",
            submitted_by_user_id=governance.operator.user_id,
            actor_role_snapshot=[RoleName.OPERATOR.value],
            supersedes_request_id=first_id,
        )
        checksum = canonical_payload_hash(payload)
        session.add(
            SourcePermissionRequest(
                id=uuid5(
                    NAMESPACE_URL,
                    "magicforge:source-permission-request:"
                    f"{version.id}:3:{checksum}",
                ),
                source_version_id=version.id,
                sequence_number=3,
                requested_extraction_permission=ExtractionPermission.FULL_TEXT,
                requested_storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
                requested_scope_locators=[],
                rights_basis="A syntactically valid but incorrectly linked request.",
                rights_evidence=["https://example.test/rights/chain-third"],
                reason="Exercise the complete supersession-chain integrity gate.",
                submitted_by_user_id=governance.operator.user_id,
                actor_role_snapshot=[RoleName.OPERATOR.value],
                domain_schema_version="source-permission-request-0.1",
                request_checksum=checksum,
                supersedes_request_id=first_id,
                created_at=NOW + timedelta(seconds=2),
            )
        )
        session.commit()

    with pytest.raises(GovernanceConflictError) as chain:
        governance.service.get_source(governance.reviewer, source.id)
    assert chain.value.code == "source_permission_request_chain_invalid"


def test_source_detail_normalizes_invalid_legacy_request_metadata(
    governance,
) -> None:
    source = governance.service.register_source(
        governance.operator,
        _source_command(slug="legacy-request-metadata"),
        idempotency_key="legacy-request-metadata-source",
    )
    version_id = source.versions[0].id
    with governance.database.session_factory() as session:
        version = session.get(SourceVersion, version_id)
        assert version is not None
        metadata = dict(version.access_metadata)
        metadata.update(
            {
                "requested_extraction_permission": "legacy-unknown-value",
                "requested_storage_permission": {"invalid": True},
                "requested_scope_locators": "not-a-list",
            }
        )
        session.execute(
            update(SourceVersion)
            .where(SourceVersion.id == version_id)
            .values(access_metadata=metadata)
        )
        session.commit()

    detail = governance.service.get_source(governance.reviewer, source.id)
    version_view = detail.versions[0]
    assert version_view.requested_extraction_permission == ExtractionPermission.NONE
    assert version_view.requested_storage_permission == StoragePermission.NONE
    assert version_view.requested_scope_locators == []


def test_permission_request_integrity_and_content_access_fail_closed(
    governance,
) -> None:
    source_command = _source_command(slug="permission-integrity")
    source = governance.service.register_source(
        governance.operator,
        source_command,
        idempotency_key="permission-integrity-source",
    )
    version_id = source.versions[0].id
    with governance.database.session_factory() as session:
        row = session.scalar(
            select(SourcePermissionRequest).where(
                SourcePermissionRequest.source_version_id == version_id
            )
        )
        assert row is not None
        session.expunge(row)
        row.rights_basis = "Tampered rights basis."
        with pytest.raises(GovernanceConflictError) as integrity:
            governance.service._permission_request_view(session, row)
    assert integrity.value.code == "source_permission_request_integrity_failed"

    with governance.database.session_factory() as session:
        row = session.scalar(
            select(SourcePermissionRequest).where(
                SourcePermissionRequest.source_version_id == version_id
            )
        )
        assert row is not None
        session.expunge(row)
        row.id = uuid4()
        with pytest.raises(GovernanceConflictError) as identity_integrity:
            governance.service._permission_request_view(session, row)
    assert identity_integrity.value.code == (
        "source_permission_request_integrity_failed"
    )

    with governance.database.session_factory() as session:
        row = session.scalar(
            select(SourcePermissionRequest).where(
                SourcePermissionRequest.source_version_id == version_id
            )
        )
        version_row = session.get(SourceVersion, version_id)
        assert row is not None and version_row is not None
        session.expunge(row)
        row.id = UUID(
            hashlib.md5(
                f"magicforge:source-permission-request:{version_id}".encode(),
                usedforsecurity=False,
            ).hexdigest()
        )
        row.actor_role_snapshot = []
        row.domain_schema_version = "source-permission-request-legacy-0.1"
        row.rights_basis = "legacy_access_metadata_snapshot_only_no_grant"
        row.rights_evidence = [
            f"source-version:{version_id}",
            f"content-sha256:{version_row.content_hash}",
        ]
        row.reason = (
            "Backfilled from immutable SourceVersion access_metadata; "
            "no permission elevation."
        )
        row.supersedes_request_id = None
        row.request_checksum = hashlib.sha256(
            (
                "magicforge:legacy-source-permission-request:"
                f"{version_id}:{version_row.content_hash}"
            ).encode()
        ).hexdigest()
        legacy_view = governance.service._permission_request_view(session, row)
        assert legacy_view.id == row.id
        row.request_checksum = "0" * 64
        with pytest.raises(GovernanceConflictError) as legacy_integrity:
            governance.service._permission_request_view(session, row)
    assert legacy_integrity.value.code == "source_permission_request_integrity_failed"

    before_versions = 0
    with governance.database.session_factory() as session:
        before_versions = _count(session, SourceVersion)
    redistribution_payload = source_command.model_dump(mode="python")
    redistribution_payload["citation"]["doi"] = (
        "10.5555/magicforge.workflow.permission-no-redistribution"
    )
    redistribution_payload["citation"]["url"] = (
        "https://example.test/papers/permission-no-redistribution"
    )
    redistribution_payload["access"]["redistribution_allowed"] = False
    redistribution_payload["citation_verification"] = []
    redistribution_command = SourceVersionCommand.model_validate(
        redistribution_payload
    )
    with pytest.raises(GovernanceConflictError) as redistribution:
        governance.service.register_source(
            governance.operator,
            redistribution_command,
            idempotency_key="permission-no-redistribution",
        )
    assert redistribution.value.code == "redistribution_not_allowed"

    abstract_payload = source_command.model_dump(mode="python")
    abstract_payload["citation"]["doi"] = (
        "10.5555/magicforge.workflow.permission-abstract"
    )
    abstract_payload["citation"]["url"] = (
        "https://example.test/papers/permission-abstract"
    )
    abstract_payload["content_access"] = ContentAccess.ABSTRACT
    abstract_payload["requested_storage_permission"] = StoragePermission.NONE
    abstract_payload["citation_verification"] = []
    abstract_command = SourceVersionCommand.model_validate(abstract_payload)
    with pytest.raises(GovernanceConflictError) as content_access:
        governance.service.register_source(
            governance.operator,
            abstract_command,
            idempotency_key="permission-abstract",
        )
    assert content_access.value.code == "claim_level_content_required"
    with governance.database.session_factory() as session:
        assert _count(session, SourceVersion) == before_versions


def test_source_mutation_responses_filter_invisible_versions(governance) -> None:
    first_command = _source_command(slug="response-clearance")
    source = governance.service.register_source(
        governance.operator,
        first_command,
        idempotency_key="response-clearance-v1",
    )
    secret = governance.service.add_source_version(
        governance.dual_role,
        source.id,
        _source_command(
            slug="response-clearance",
            content=f"{SOURCE_TEXT} Secret method appendix.",
            sensitivity=SensitiveInformationLevel.SECRET_METHOD,
        ),
        idempotency_key="response-clearance-v2",
    )
    assert any(
        version.sensitivity == SensitiveInformationLevel.SECRET_METHOD
        for version in secret.versions
    )

    visible = governance.service.add_source_version(
        governance.operator,
        source.id,
        _source_command(
            slug="response-clearance",
            content=f"{SOURCE_TEXT} Public erratum.",
        ),
        idempotency_key="response-clearance-v3",
    )

    assert [version.version for version in visible.versions] == [1, 3]
    assert all(
        version.sensitivity != SensitiveInformationLevel.SECRET_METHOD
        for version in visible.versions
    )


def test_rbac_and_independent_human_review_are_enforced(governance) -> None:
    command = _source_command(slug="rbac")
    with pytest.raises(GovernanceAuthorizationError):
        governance.service.register_source(
            governance.reader, command, idempotency_key="reader-register-forbidden"
        )

    registered = governance.service.register_source(
        governance.operator, command, idempotency_key="rbac-source"
    )
    with pytest.raises(GovernanceAuthorizationError):
        governance.service.review_source(
            governance.operator,
            registered.id,
            _source_review_command(
                registered.versions[0].id,
                source_permission_request_id=_permission_request_id(
                    governance, registered.versions[0].id
                ),
            ),
            idempotency_key="operator-review-forbidden",
        )

    own_source = governance.service.register_source(
        governance.dual_role,
        _source_command(slug="self-review-source"),
        idempotency_key="dual-source",
    )
    with pytest.raises(GovernanceConflictError) as source_self_review:
        governance.service.review_source(
            governance.dual_role,
            own_source.id,
            _source_review_command(
                own_source.versions[0].id,
                source_permission_request_id=_permission_request_id(
                    governance, own_source.versions[0].id
                ),
            ),
            idempotency_key="dual-source-review",
        )
    assert source_self_review.value.code == "self_review_forbidden"

    governance.service.review_source(
        governance.reviewer,
        registered.id,
        _source_review_command(
            registered.versions[0].id,
            source_permission_request_id=_permission_request_id(
                governance, registered.versions[0].id
            ),
            citation_verification=command.citation_verification,
        ),
        idempotency_key="rbac-source-approved",
    )
    own_claim = _submit_claim(
        governance,
        registered.versions[0].id,
        key="dual-claim",
        actor=governance.dual_role,
    )
    with pytest.raises(GovernanceConflictError) as claim_self_review:
        governance.service.review_claim(
            governance.dual_role,
            own_claim.id,
            _claim_review_command(governance.dual_role.username),
            idempotency_key="dual-claim-review",
        )
    assert claim_self_review.value.code == "self_review_forbidden"


def test_automated_identity_cannot_create_human_review_decisions(governance) -> None:
    _source_id, approved_version_id = _register_and_approve_source(
        governance,
        slug="automated-review-claim-source",
    )
    claim = _submit_claim(
        governance,
        approved_version_id,
        key="automated-review-claim",
    )
    pending_command = _source_command(slug="automated-review-source")
    pending_source = governance.service.register_source(
        governance.operator,
        pending_command,
        idempotency_key="automated-review-source-register",
    )

    assert governance.reviewer.user_id is not None
    with governance.database.session_factory() as session:
        reviewer = session.get(User, governance.reviewer.user_id)
        assert reviewer is not None
        reviewer.username = "glm"
        reviewer.username_normalized = "glm"
        session.commit()

    with governance.database.session_factory() as session:
        before = {
            "source_decisions": _count(session, SourceReviewDecision),
            "claim_decisions": _count(session, ClaimReviewDecision),
            "audits": _count(session, AuditEvent),
            "idempotency": _count(session, IdempotencyRecord),
        }

    with pytest.raises(GovernanceAuthorizationError) as source_rejected:
        governance.service.review_source(
            governance.reviewer,
            pending_source.id,
            _source_review_command(
                pending_source.versions[0].id,
                source_permission_request_id=_permission_request_id(
                    governance, pending_source.versions[0].id
                ),
                citation_verification=pending_command.citation_verification,
            ),
            idempotency_key="automated-source-review-denied",
        )
    assert source_rejected.value.code == "human_reviewer_required"

    with pytest.raises(GovernanceAuthorizationError) as claim_rejected:
        governance.service.review_claim(
            governance.reviewer,
            claim.id,
            _claim_review_command("glm"),
            idempotency_key="automated-claim-review-denied",
        )
    assert claim_rejected.value.code == "human_reviewer_required"

    with governance.database.session_factory() as session:
        assert _count(session, SourceReviewDecision) == before["source_decisions"]
        assert _count(session, ClaimReviewDecision) == before["claim_decisions"]
        assert _count(session, AuditEvent) == before["audits"]
        assert _count(session, IdempotencyRecord) == before["idempotency"]
        assert (
            _count(
                session,
                IdempotencyRecord,
                IdempotencyRecord.idempotency_key.in_(
                    {
                        "automated-source-review-denied",
                        "automated-claim-review-denied",
                    }
                ),
            )
            == 0
        )


def test_citation_status_is_derived_from_immutable_verification_evidence(
    governance,
) -> None:
    registration_expected = {
        "none": "unverified",
        "mismatch": "failed",
        "metadata": "unverified",
        "full_text": "unverified",
    }
    registered = {}
    commands = {}
    for verification, citation_status in registration_expected.items():
        command = _source_command(
            slug=f"citation-{verification}", verification=verification
        )
        commands[verification] = command
        source = governance.service.register_source(
            governance.operator,
            command,
            idempotency_key=f"citation-{verification}",
        )
        registered[verification] = source
        assert source.versions[0].citation_status == citation_status

    for verification in ("none", "mismatch"):
        source = registered[verification]
        with pytest.raises(GovernanceConflictError) as rejected:
            governance.service.review_source(
                governance.reviewer,
                source.id,
                _source_review_command(
                    source.versions[0].id,
                    source_permission_request_id=_permission_request_id(
                        governance, source.versions[0].id
                    ),
                ),
                idempotency_key=f"approve-unverified-{verification}",
            )
        assert rejected.value.code == "citation_not_verified"

    for verification, expected_status in (
        ("metadata", "metadata_verified"),
        ("full_text", "full_text_verified"),
    ):
        source = registered[verification]
        approved = governance.service.review_source(
            governance.reviewer,
            source.id,
            _source_review_command(
                source.versions[0].id,
                source_permission_request_id=_permission_request_id(
                    governance, source.versions[0].id
                ),
                citation_verification=commands[verification].citation_verification,
            ),
            idempotency_key=f"approve-verified-{verification}",
        )
        assert approved.versions[0].citation_status == expected_status

    with governance.database.session_factory() as session:
        rejected_version_ids = {
            registered[name].versions[0].id for name in ("none", "mismatch")
        }
        assert (
            _count(
                session,
                SourceReviewDecision,
                SourceReviewDecision.source_version_id.in_(rejected_version_ids),
            )
            == 0
        )


def test_source_review_cannot_expand_selected_section_permission(governance) -> None:
    command = _source_command(
        slug="selected-scope",
        extraction_permission=ExtractionPermission.SELECTED_SECTIONS,
        requested_scope_locators=["Results"],
    )
    source = governance.service.register_source(
        governance.operator,
        command,
        idempotency_key="selected-scope-source",
    )
    version_id = source.versions[0].id

    with pytest.raises(GovernanceConflictError) as exceeded:
        governance.service.review_source(
            governance.reviewer,
            source.id,
            _source_review_command(
                version_id,
                source_permission_request_id=_permission_request_id(
                    governance, version_id
                ),
                extraction_permission=ExtractionPermission.SELECTED_SECTIONS,
                extraction_scope_locators=["Methods"],
                citation_verification=command.citation_verification,
            ),
            idempotency_key="selected-scope-exceeded",
        )
    assert exceeded.value.code == "extraction_scope_exceeded"

    approved = governance.service.review_source(
        governance.reviewer,
        source.id,
        _source_review_command(
            version_id,
            source_permission_request_id=_permission_request_id(
                governance, version_id
            ),
            extraction_permission=ExtractionPermission.SELECTED_SECTIONS,
            extraction_scope_locators=["Results"],
            citation_verification=command.citation_verification,
        ),
        idempotency_key="selected-scope-approved",
    )
    assert approved.versions[0].status == WorkflowStatus.APPROVED


def test_write_paths_enforce_source_sensitivity_clearance(governance) -> None:
    secret_command = _source_command(
        slug="secret-source",
        sensitivity=SensitiveInformationLevel.SECRET_METHOD,
    )
    with pytest.raises(GovernanceAuthorizationError):
        governance.service.register_source(
            governance.operator,
            secret_command,
            idempotency_key="operator-secret-register",
        )

    source = governance.service.register_source(
        governance.dual_role,
        secret_command,
        idempotency_key="dual-secret-register",
    )
    version_id = source.versions[0].id
    governance.service.review_source(
        governance.reviewer,
        source.id,
        _source_review_command(
            version_id,
            source_permission_request_id=_permission_request_id(
                governance, version_id
            ),
            sensitivity=SensitiveInformationLevel.SECRET_METHOD,
            citation_verification=secret_command.citation_verification,
        ),
        idempotency_key="secret-source-review",
    )

    with pytest.raises(GovernanceAuthorizationError):
        _submit_claim(
            governance,
            version_id,
            key="operator-secret-claim",
            actor=governance.operator,
        )


def test_invalid_source_and_claim_transitions_do_not_append_decisions(
    governance,
) -> None:
    source_id, version_id = _register_and_approve_source(
        governance, slug="invalid-transition"
    )
    with pytest.raises(GovernanceConflictError) as source_transition:
        governance.service.review_source(
            governance.reviewer,
            source_id,
            _source_review_command(
                version_id,
                source_permission_request_id=_permission_request_id(
                    governance, version_id
                ),
                decision=ReviewDecision.REJECT,
            ),
            idempotency_key="source-second-decision",
        )
    assert source_transition.value.code == "invalid_state_transition"

    claim = _submit_claim(
        governance, version_id, key="invalid-transition-claim"
    )
    governance.service.review_claim(
        governance.reviewer,
        claim.id,
        _claim_review_command(governance.reviewer.username),
        idempotency_key="claim-first-decision",
    )
    with pytest.raises(GovernanceConflictError) as claim_transition:
        governance.service.review_claim(
            governance.reviewer,
            claim.id,
            _claim_review_command(
                governance.reviewer.username, decision=ReviewDecision.REJECT
            ),
            idempotency_key="claim-second-decision",
        )
    assert claim_transition.value.code == "invalid_state_transition"

    with governance.database.session_factory() as session:
        assert (
            _count(
                session,
                SourceReviewDecision,
                SourceReviewDecision.source_version_id == version_id,
            )
            == 1
        )
        assert (
            _count(
                session,
                ClaimReviewDecision,
                ClaimReviewDecision.claim_candidate_id == claim.id,
            )
            == 1
        )


def test_context_only_claim_cannot_create_evidence(governance) -> None:
    source_id, version_id = _register_and_approve_source(
        governance, slug="context-only"
    )
    claim = _submit_claim(
        governance,
        version_id,
        key="context-only-claim",
        role=ClaimRole.CONTEXT_ONLY,
    )
    with pytest.raises(GovernanceConflictError) as rejected:
        governance.service.review_claim(
            governance.reviewer,
            claim.id,
            _claim_review_command(governance.reviewer.username),
            idempotency_key="context-only-review",
        )
    assert rejected.value.code == "context_only_not_evidence"

    with governance.database.session_factory() as session:
        stored = session.get(ClaimCandidate, claim.id)
        assert stored is not None
        assert stored.status == GovernanceWorkflowStatus.SUBMITTED
        assert not stored.evidence_card_eligible
        assert _count(session, ClaimReviewDecision) == 0
        assert _count(session, EvidenceCardVersion) == 0


def test_claim_approval_atomically_creates_a_valid_evidence_card(governance) -> None:
    _source_id, version_id = _register_and_approve_source(
        governance, slug="atomic-evidence"
    )
    claim = _submit_claim(governance, version_id, key="atomic-claim")
    result = governance.service.review_claim(
        governance.reviewer,
        claim.id,
        _claim_review_command(governance.reviewer.username),
        idempotency_key="atomic-claim-approval",
        request_id="atomic-review-request",
        correlation_id="atomic-review-correlation",
    )

    assert result.claim.status == WorkflowStatus.APPROVED
    assert result.evidence_card_id == claim.id
    assert result.evidence_version_id is not None
    with governance.database.session_factory() as session:
        candidate = session.get(ClaimCandidate, claim.id)
        decision = session.scalar(
            select(ClaimReviewDecision).where(
                ClaimReviewDecision.claim_candidate_id == claim.id
            )
        )
        version = session.get(EvidenceCardVersion, result.evidence_version_id)
        assert candidate is not None
        assert candidate.status == GovernanceWorkflowStatus.APPROVED
        assert decision is not None
        assert version is not None
        card = EvidenceCard.model_validate(version.payload)
        assert card.review.approved
        assert card.review.review_item_id == str(
            uuid5(
                NAMESPACE_URL,
                f"magicforge:claim-review:{card.id}:{card.version}",
            )
        )
        assert version.claim_review_decision_id == decision.id
        assert version.claim_review_decision_id == decision.id
        assert version.claim_candidate_id == claim.id
        assert version.payload_checksum == canonical_payload_hash(version.payload)
        assert (
            _count(
                session,
                AuditEvent,
                AuditEvent.event_type == "claim_approved",
                AuditEvent.object_id == str(claim.id),
            )
            == 1
        )


def test_audit_failure_rolls_back_claim_decision_evidence_and_idempotency(
    governance,
) -> None:
    _source_id, version_id = _register_and_approve_source(
        governance, slug="audit-rollback"
    )
    claim = _submit_claim(governance, version_id, key="rollback-claim")

    def fail_audit_factory(_session):
        raise RuntimeError("simulated audit persistence failure")

    failing_service = ProductionGovernanceService(
        governance.database.session_factory,
        clock=lambda: NOW,
        audit_repository_factory=fail_audit_factory,
    )
    with governance.database.session_factory() as session:
        before = {
            "decisions": _count(session, ClaimReviewDecision),
            "versions": _count(session, EvidenceCardVersion),
            "audits": _count(session, AuditEvent),
            "idempotency": _count(session, IdempotencyRecord),
        }

    with pytest.raises(RuntimeError, match="simulated audit"):
        failing_service.review_claim(
            governance.reviewer,
            claim.id,
            _claim_review_command(governance.reviewer.username),
            idempotency_key="claim-review-audit-failure",
        )

    with governance.database.session_factory() as session:
        candidate = session.get(ClaimCandidate, claim.id)
        assert candidate is not None
        assert candidate.status == GovernanceWorkflowStatus.SUBMITTED
        assert _count(session, ClaimReviewDecision) == before["decisions"]
        assert _count(session, EvidenceCardVersion) == before["versions"]
        assert _count(session, AuditEvent) == before["audits"]
        assert _count(session, IdempotencyRecord) == before["idempotency"]
        assert (
            _count(
                session,
                IdempotencyRecord,
                IdempotencyRecord.idempotency_key
                == "claim-review-audit-failure",
            )
            == 0
        )


def test_idempotent_replay_is_stable_and_changed_payload_conflicts(governance) -> None:
    command = _source_command(slug="idempotency")
    first = governance.service.register_source(
        governance.operator, command, idempotency_key="stable-register-key"
    )
    replay = governance.service.register_source(
        governance.operator, command, idempotency_key="stable-register-key"
    )
    assert replay == first

    changed = _source_command(
        slug="idempotency", content=f"{SOURCE_TEXT} Additional material."
    )
    with pytest.raises(GovernanceConflictError) as conflict:
        governance.service.register_source(
            governance.operator,
            changed,
            idempotency_key="stable-register-key",
        )
    assert conflict.value.code == "idempotency_conflict"

    with governance.database.session_factory() as session:
        assert _count(session, SourceVersion) == 1
        assert (
            _count(
                session,
                IdempotencyRecord,
                IdempotencyRecord.idempotency_key == "stable-register-key",
            )
            == 1
        )


def test_all_governance_mutations_have_strict_idempotency(governance) -> None:
    source_command = _source_command(slug="all-idempotency")
    source = governance.service.register_source(
        governance.operator,
        source_command,
        idempotency_key="all-idempotency-source",
    )
    version_id = source.versions[0].id
    review_command = _source_review_command(
        version_id,
        source_permission_request_id=_permission_request_id(
            governance, version_id
        ),
        citation_verification=source_command.citation_verification,
    )
    source_review = governance.service.review_source(
        governance.reviewer,
        source.id,
        review_command,
        idempotency_key="all-idempotency-source-review",
    )
    assert governance.service.review_source(
        governance.reviewer,
        source.id,
        review_command,
        idempotency_key="all-idempotency-source-review",
    ) == source_review
    with pytest.raises(GovernanceConflictError) as source_review_conflict:
        governance.service.review_source(
            governance.reviewer,
            source.id,
            review_command.model_copy(update={"reason": "A different review reason."}),
            idempotency_key="all-idempotency-source-review",
        )
    assert source_review_conflict.value.code == "idempotency_conflict"

    claim_command = _claim_command(version_id)
    claim = governance.service.submit_claim(
        governance.operator,
        claim_command,
        idempotency_key="all-idempotency-claim",
    )
    assert governance.service.submit_claim(
        governance.operator,
        claim_command,
        idempotency_key="all-idempotency-claim",
    ) == claim
    with pytest.raises(GovernanceConflictError) as claim_conflict:
        governance.service.submit_claim(
            governance.operator,
            _claim_command(version_id, claim=SECOND_CLAIM_TEXT),
            idempotency_key="all-idempotency-claim",
        )
    assert claim_conflict.value.code == "idempotency_conflict"

    claim_review_command = _claim_review_command(governance.reviewer.username)
    claim_review = governance.service.review_claim(
        governance.reviewer,
        claim.id,
        claim_review_command,
        idempotency_key="all-idempotency-claim-review",
    )
    assert governance.service.review_claim(
        governance.reviewer,
        claim.id,
        claim_review_command,
        idempotency_key="all-idempotency-claim-review",
    ) == claim_review
    changed_review = ClaimReviewCommand.model_validate(
        {
            **claim_review_command.model_dump(mode="python"),
            "confidence": {
                **claim_review_command.confidence.model_dump(mode="python"),
                "assessed_at": NOW + timedelta(seconds=1),
            },
        }
    )
    with pytest.raises(GovernanceConflictError) as claim_review_conflict:
        governance.service.review_claim(
            governance.reviewer,
            claim.id,
            changed_review,
            idempotency_key="all-idempotency-claim-review",
        )
    assert claim_review_conflict.value.code == "idempotency_conflict"


def test_idempotent_replay_rechecks_current_sensitivity_clearance(governance) -> None:
    source_command = _source_command(
        slug="replay-clearance",
        sensitivity=SensitiveInformationLevel.SECRET_METHOD,
    )
    source = governance.service.register_source(
        governance.dual_role,
        source_command,
        idempotency_key="replay-clearance-source",
    )
    version_id = source.versions[0].id
    governance.service.review_source(
        governance.reviewer,
        source.id,
        _source_review_command(
            version_id,
            source_permission_request_id=_permission_request_id(
                governance, version_id
            ),
            sensitivity=SensitiveInformationLevel.SECRET_METHOD,
            citation_verification=source_command.citation_verification,
        ),
        idempotency_key="replay-clearance-source-review",
    )
    claim_command = _claim_command(version_id)
    governance.service.submit_claim(
        governance.dual_role,
        claim_command,
        idempotency_key="replay-clearance-claim",
    )

    assert governance.dual_role.user_id is not None
    assert governance.reviewer.user_id is not None
    with governance.database.session_factory() as session:
        reviewer_role = session.scalar(
            select(Role).where(Role.name == RoleName.REVIEWER)
        )
        assert reviewer_role is not None
        assignment = session.get(
            UserRole,
            (governance.dual_role.user_id, reviewer_role.id),
        )
        assert assignment is not None and assignment.revoked_at is None
        assignment.revoked_at = NOW + timedelta(seconds=1)
        assignment.revoked_by_user_id = governance.reviewer.user_id
        session.commit()

    with pytest.raises(GovernanceAuthorizationError):
        governance.service.register_source(
            governance.dual_role,
            source_command,
            idempotency_key="replay-clearance-source",
        )
    with pytest.raises(GovernanceAuthorizationError):
        governance.service.submit_claim(
            governance.dual_role,
            claim_command,
            idempotency_key="replay-clearance-claim",
        )

    with governance.database.session_factory() as session:
        completed = list(
            session.scalars(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.idempotency_key.in_(
                        {"replay-clearance-source", "replay-clearance-claim"}
                    )
                )
            )
        )
        assert len(completed) == 2
        assert all(record.response_body is not None for record in completed)


def test_effective_source_sensitivity_escalation_blocks_detail_and_replays(
    governance,
) -> None:
    normal_reviewer, elevated_reviewer = _break_glass_reviewer(governance)
    source_command = _source_command(
        slug="effective-source-sensitivity",
        sensitivity=SensitiveInformationLevel.CONTROLLED,
    )
    source = governance.service.register_source(
        governance.operator,
        source_command,
        idempotency_key="effective-source-register",
    )
    version_id = source.versions[0].id
    review_command = _source_review_command(
        version_id,
        source_permission_request_id=_permission_request_id(
            governance, version_id
        ),
        sensitivity=SensitiveInformationLevel.RESTRICTED,
        citation_verification=source_command.citation_verification,
    )
    approved = governance.service.review_source(
        elevated_reviewer,
        source.id,
        review_command,
        idempotency_key="effective-source-review",
    )
    assert approved.versions[0].sensitivity == SensitiveInformationLevel.RESTRICTED

    with pytest.raises(GovernanceNotFoundError):
        governance.service.get_source(governance.reviewer, source.id)
    with pytest.raises(GovernanceAuthorizationError):
        governance.service.register_source(
            governance.operator,
            source_command,
            idempotency_key="effective-source-register",
        )
    with pytest.raises(GovernanceAuthorizationError):
        governance.service.review_source(
            normal_reviewer,
            source.id,
            review_command,
            idempotency_key="effective-source-review",
        )

    assert governance.service.review_source(
        elevated_reviewer,
        source.id,
        review_command,
        idempotency_key="effective-source-review",
    ) == approved


def test_effective_claim_sensitivity_escalation_blocks_views_and_replays(
    governance,
) -> None:
    normal_reviewer, elevated_reviewer = _break_glass_reviewer(governance)
    source_id, version_id = _register_and_approve_source(
        governance,
        slug="effective-claim-sensitivity",
        sensitivity=SensitiveInformationLevel.CONTROLLED,
    )
    claim_command = _claim_command(version_id)
    submitted = governance.service.submit_claim(
        governance.operator,
        claim_command,
        idempotency_key="effective-claim-submit",
    )
    review_command = _claim_review_command(
        normal_reviewer.username,
        sensitivity=SensitiveInformationLevel.RESTRICTED,
    )
    approved = governance.service.review_claim(
        elevated_reviewer,
        submitted.id,
        review_command,
        idempotency_key="effective-claim-review",
    )
    assert approved.claim.sensitivity == SensitiveInformationLevel.RESTRICTED
    assert approved.evidence_card_id is not None

    with pytest.raises(GovernanceNotFoundError):
        governance.service.get_claim(governance.reviewer, submitted.id)
    with pytest.raises(GovernanceNotFoundError):
        governance.service.get_source(governance.reviewer, source_id)
    assert governance.service.list_claims(governance.reviewer) == []
    with pytest.raises(GovernanceNotFoundError):
        governance.service.list_evidence_versions(
            governance.reviewer,
            approved.evidence_card_id,
        )
    with pytest.raises(GovernanceAuthorizationError):
        governance.service.submit_claim(
            governance.operator,
            claim_command,
            idempotency_key="effective-claim-submit",
        )
    with pytest.raises(GovernanceAuthorizationError):
        governance.service.review_claim(
            normal_reviewer,
            submitted.id,
            review_command,
            idempotency_key="effective-claim-review",
        )

    assert governance.service.review_claim(
        elevated_reviewer,
        submitted.id,
        review_command,
        idempotency_key="effective-claim-review",
    ) == approved


def test_review_queues_filter_sensitive_rows_before_pagination(governance) -> None:
    """A hidden first row must not consume the reviewer's requested page."""

    _normal_admin, elevated_reviewer = _break_glass_reviewer(governance)
    governance.service._clock = lambda: NOW
    _restricted_source_id, restricted_version_id = _register_and_approve_source(
        governance, slug="queue-restricted-first"
    )
    restricted_claim = _submit_claim(
        governance,
        restricted_version_id,
        key="queue-restricted-first-claim",
    )
    governance.service.review_claim(
        elevated_reviewer,
        restricted_claim.id,
        _claim_review_command(
            elevated_reviewer.username,
            sensitivity=SensitiveInformationLevel.RESTRICTED,
        ),
        idempotency_key="queue-restricted-first-review",
    )

    governance.service._clock = lambda: NOW + timedelta(minutes=1)
    _visible_source_id, visible_version_id = _register_and_approve_source(
        governance, slug="queue-visible-second"
    )
    visible_claim = _submit_claim(
        governance,
        visible_version_id,
        key="queue-visible-second-claim",
        claim=SECOND_CLAIM_TEXT,
    )

    source_page = governance.service.list_sources(
        governance.reviewer, limit=1, offset=0
    )
    assert [item.source_version_id for item in source_page] == [visible_version_id]
    claim_page = governance.service.list_claims(
        governance.reviewer, limit=1, offset=0
    )
    assert [item.id for item in claim_page] == [visible_claim.id]


def test_claim_queue_hides_every_sibling_after_effective_source_escalation(
    governance,
) -> None:
    """A restricted sibling Claim raises the whole raw Source boundary."""

    _normal_admin, elevated_reviewer = _break_glass_reviewer(governance)
    source_id, version_id = _register_and_approve_source(
        governance, slug="queue-sibling-escalation"
    )
    visible_claim = _submit_claim(
        governance,
        version_id,
        key="queue-sibling-visible",
        claim=CLAIM_TEXT,
    )
    restricted_claim = _submit_claim(
        governance,
        version_id,
        key="queue-sibling-restricted",
        claim=SECOND_CLAIM_TEXT,
    )
    governance.service.review_claim(
        elevated_reviewer,
        restricted_claim.id,
        _claim_review_command(
            elevated_reviewer.username,
            sensitivity=SensitiveInformationLevel.RESTRICTED,
        ),
        idempotency_key="queue-sibling-restricted-review",
    )

    assert governance.service.list_claims(governance.reviewer) == []
    with pytest.raises(GovernanceNotFoundError):
        governance.service.get_claim(governance.reviewer, visible_claim.id)
    with pytest.raises(GovernanceNotFoundError):
        governance.service.get_source(governance.reviewer, source_id)


def test_evidence_read_rejects_checksum_tampering_and_effective_source_escalation(
    governance,
) -> None:
    _normal_admin, elevated_reviewer = _break_glass_reviewer(governance)
    _source_id, version_id = _register_and_approve_source(
        governance, slug="evidence-current-read"
    )
    first_claim = _submit_claim(
        governance,
        version_id,
        key="evidence-current-first",
        claim=CLAIM_TEXT,
    )
    first_approval = governance.service.review_claim(
        governance.reviewer,
        first_claim.id,
        _claim_review_command(governance.reviewer.username),
        idempotency_key="evidence-current-first-review",
    )
    assert first_approval.evidence_version_id is not None

    sibling = _submit_claim(
        governance,
        version_id,
        key="evidence-current-sibling",
        claim=SECOND_CLAIM_TEXT,
    )
    governance.service.review_claim(
        elevated_reviewer,
        sibling.id,
        _claim_review_command(
            elevated_reviewer.username,
            sensitivity=SensitiveInformationLevel.RESTRICTED,
        ),
        idempotency_key="evidence-current-sibling-review",
    )
    with pytest.raises(GovernanceNotFoundError):
        governance.service.list_evidence_versions(
            governance.reader, first_approval.evidence_version_id
        )

    # Even a fully elevated actor must not receive a checksum-invalid row.
    with governance.database.session_factory() as session:
        row = session.get(EvidenceCardVersion, first_approval.evidence_version_id)
        assert row is not None
        session.expunge(row)
        row.payload = {
            **row.payload,
            "claim": "Tampered but schema-valid claim.",
        }
        assert governance.service._current_evidence_card(session, row) is None


def test_explicit_provenance_timestamp_is_part_of_idempotency_payload(
    governance,
) -> None:
    command = _source_command(slug="explicit-time-idempotency")
    governance.service.register_source(
        governance.operator,
        command,
        idempotency_key="explicit-time-source",
    )
    payload = command.model_dump(mode="python")
    payload["citation"]["provenance"][0]["retrieved_at"] = NOW + timedelta(seconds=1)
    changed = SourceVersionCommand.model_validate(payload)

    with pytest.raises(GovernanceConflictError) as conflict:
        governance.service.register_source(
            governance.operator,
            changed,
            idempotency_key="explicit-time-source",
        )
    assert conflict.value.code == "idempotency_conflict"


def test_insufficient_confidence_is_a_controlled_input_error(governance) -> None:
    _source_id, version_id = _register_and_approve_source(
        governance,
        slug="insufficient-confidence",
    )
    claim = _submit_claim(
        governance,
        version_id,
        key="insufficient-confidence-claim",
    )
    payload = _claim_review_command(
        governance.reviewer.username
    ).model_dump(mode="python")
    for field in (
        "provenance_quality",
        "method_rigor",
        "claim_directness",
        "consistency",
        "magic_applicability",
    ):
        payload["confidence"][field]["score"] = 0.0
    command = ClaimReviewCommand.model_validate(payload)

    with pytest.raises(GovernanceInputError) as rejected:
        governance.service.review_claim(
            governance.reviewer,
            claim.id,
            command,
            idempotency_key="insufficient-confidence-review",
        )
    assert rejected.value.code == "confidence_insufficient"


def test_sensitivity_cannot_be_downgraded_and_projection_is_public_safe(
    governance,
) -> None:
    _source_id, version_id = _register_and_approve_source(
        governance,
        slug="safe-projection",
        storage_permission=StoragePermission.DERIVED_WITH_SHORT_EXCERPT,
        sensitivity=SensitiveInformationLevel.CONTROLLED,
    )
    claim = _submit_claim(governance, version_id, key="safe-projection-claim")
    with pytest.raises(GovernanceConflictError) as downgrade:
        governance.service.review_claim(
            governance.reviewer,
            claim.id,
            _claim_review_command(
                governance.reviewer.username,
                storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
                sensitivity=SensitiveInformationLevel.PUBLIC,
            ),
            idempotency_key="unsafe-sensitivity-downgrade",
        )
    assert downgrade.value.code == "sensitivity_downgrade_forbidden"

    approved = governance.service.review_claim(
        governance.reviewer,
        claim.id,
        _claim_review_command(
            governance.reviewer.username,
            storage_permission=StoragePermission.DERIVED_KNOWLEDGE_ONLY,
            sensitivity=SensitiveInformationLevel.CONTROLLED,
        ),
        idempotency_key="safe-projection-approval",
    )
    assert approved.evidence_card_id is not None
    assert approved.evidence_version_id is not None
    views = governance.service.list_evidence_versions(
        governance.reader, approved.evidence_card_id
    )
    assert len(views) == 1
    view = views[0]
    assert view.claim == CLAIM_TEXT
    assert view.source_locator is None
    assert view.evidence_excerpt is None
    assert view.sensitivity == SensitiveInformationLevel.CONTROLLED
    public_payload = view.model_dump(mode="json")
    assert "reviewer" not in public_payload
    assert "approval_reason" not in public_payload
    assert "review_item_id" not in public_payload

    exact_row_views = governance.service.list_evidence_versions(
        governance.reader, approved.evidence_version_id
    )
    assert [item.evidence_version_id for item in exact_row_views] == [
        approved.evidence_version_id
    ]

    with pytest.raises(GovernanceNotFoundError):
        governance.service.list_evidence_versions(
            governance.reader,
            # An unknown family must fail closed rather than disclose metadata.
            uuid4(),
        )


def test_claim_candidate_insert_rejects_terminal_initial_status(governance) -> None:
    _source_id, source_version_id = _register_and_approve_source(
        governance, slug="initial-claim-status"
    )
    submitted = _submit_claim(
        governance,
        source_version_id,
        key="initial-claim-status-submitted",
    )

    with governance.database.session_factory() as session:
        template = session.get(ClaimCandidate, submitted.id)
        assert template is not None
        forged = ClaimCandidate(
            id=uuid4(),
            source_version_id=template.source_version_id,
            canonical_claim_id=uuid4(),
            claim_text="A terminal Claim must not be created without review.",
            claim_role=template.claim_role,
            claim_polarity=template.claim_polarity,
            locator=dict(template.locator),
            extraction_provenance=dict(template.extraction_provenance),
            model_run_metadata=dict(template.model_run_metadata),
            proposed_evidence_card=dict(template.proposed_evidence_card),
            candidate_schema_version=template.candidate_schema_version,
            candidate_checksum="f" * 64,
            extraction_confidence=template.extraction_confidence,
            evidence_card_eligible=template.evidence_card_eligible,
            status=GovernanceWorkflowStatus.APPROVED,
            submitted_by_user_id=template.submitted_by_user_id,
            created_by_kind=template.created_by_kind,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(forged)
        with pytest.raises(AppendOnlyMutationError, match="initial status"):
            session.commit()
        session.rollback()


def test_claim_status_must_match_latest_review_decision(governance) -> None:
    _source_id, source_version_id = _register_and_approve_source(
        governance, slug="latest-claim-decision"
    )
    submitted = _submit_claim(
        governance,
        source_version_id,
        key="latest-claim-decision-submitted",
    )

    with governance.database.session_factory() as session:
        candidate = session.get(ClaimCandidate, submitted.id)
        assert candidate is not None
        rejected = _terminal_claim_decision(
            candidate,
            reviewer_user_id=governance.reviewer.user_id,
            sequence_number=1,
            status=GovernanceWorkflowStatus.REJECTED,
        )
        session.add(rejected)
        session.flush()
        candidate.status = GovernanceWorkflowStatus.REJECTED
        session.commit()
        rejected_id = rejected.id

    with governance.database.session_factory() as session:
        candidate = session.get(ClaimCandidate, submitted.id)
        assert candidate is not None
        revoked = _terminal_claim_decision(
            candidate,
            reviewer_user_id=governance.reviewer.user_id,
            sequence_number=2,
            status=GovernanceWorkflowStatus.REVOKED,
            supersedes_decision_id=rejected_id,
        )
        session.add(revoked)
        session.flush()
        candidate.status = GovernanceWorkflowStatus.REVOKED
        session.commit()

    with governance.database.session_factory() as session:
        candidate = session.get(ClaimCandidate, submitted.id)
        assert candidate is not None
        candidate.status = GovernanceWorkflowStatus.REJECTED
        with pytest.raises(AppendOnlyMutationError, match="latest append-only"):
            session.commit()
        session.rollback()


def test_audit_decision_source_and_evidence_versions_are_append_only(
    governance,
) -> None:
    _source_id, source_version_id = _register_and_approve_source(
        governance, slug="append-only"
    )
    claim = _submit_claim(governance, source_version_id, key="append-only-claim")
    result = governance.service.review_claim(
        governance.reviewer,
        claim.id,
        _claim_review_command(governance.reviewer.username),
        idempotency_key="append-only-review",
    )
    assert result.evidence_version_id is not None

    with governance.database.session_factory() as session:
        source_version = session.get(SourceVersion, source_version_id)
        assert source_version is not None
        source_version.content_text = "Rewritten source text must be rejected."
        with pytest.raises(AppendOnlyMutationError, match="immutable"):
            session.commit()
        session.rollback()

    with governance.database.session_factory() as session:
        candidate = session.get(ClaimCandidate, claim.id)
        assert candidate is not None
        candidate.status = GovernanceWorkflowStatus.REJECTED
        with pytest.raises(AppendOnlyMutationError, match="review decision"):
            session.commit()
        session.rollback()

    with governance.database.session_factory() as session:
        candidate = session.get(ClaimCandidate, claim.id)
        assert candidate is not None
        candidate.claim_text = "A rewritten Claim must be rejected."
        with pytest.raises(AppendOnlyMutationError, match="protected fields"):
            session.commit()
        session.rollback()

    with governance.database.session_factory() as session:
        decision = session.scalar(
            select(ClaimReviewDecision).where(
                ClaimReviewDecision.claim_candidate_id == claim.id
            )
        )
        assert decision is not None
        decision.reason = "A rewritten decision reason must be rejected."
        with pytest.raises(AppendOnlyMutationError, match="immutable"):
            session.commit()
        session.rollback()

    with governance.database.session_factory() as session:
        audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "claim_approved",
                AuditEvent.object_id == str(claim.id),
            )
        )
        assert audit is not None
        audit.reason = "A rewritten audit reason must be rejected."
        with pytest.raises(AppendOnlyMutationError, match="append-only"):
            session.commit()
        session.rollback()

    with governance.database.session_factory() as session:
        evidence_version = session.get(EvidenceCardVersion, result.evidence_version_id)
        assert evidence_version is not None
        session.delete(evidence_version)
        with pytest.raises(AppendOnlyMutationError, match="immutable"):
            session.commit()
        session.rollback()

    with governance.database.session_factory() as session:
        source_version = session.get(SourceVersion, source_version_id)
        evidence_version = session.get(EvidenceCardVersion, result.evidence_version_id)
        assert source_version is not None and source_version.content_text == SOURCE_TEXT
        assert evidence_version is not None
        assert _count(session, ClaimReviewDecision) == 1
        assert (
            _count(
                session,
                AuditEvent,
                AuditEvent.event_type == "claim_approved",
                AuditEvent.object_id == str(claim.id),
            )
            == 1
        )
