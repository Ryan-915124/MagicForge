"""HTTP contract tests for the Checkpoint 3 Source/Claim workflow."""

from __future__ import annotations

from hashlib import sha256
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

import app.auth_routes as auth_routes_module
import app.governance_routes as governance_routes_module
import app.security_runtime as security_runtime_module
from app.config import Settings
from app.main import create_app
from app.runtime import RuntimeInitializationError
from knowledge.governance import MagicForgeMode
from persistence import (
    AuditEvent,
    Base,
    ClaimReviewDecision,
    DatabaseManager,
    IdempotencyRecord,
    Role,
    RoleName,
    SessionTransport,
    SourceReviewDecision,
)


PASSWORDS = {
    "admin": "a carefully chosen admin passphrase",
    "operator": "a carefully chosen operator passphrase",
    "reviewer": "a carefully chosen reviewer passphrase",
    "reader": "a carefully chosen reader passphrase",
}


class _SQLiteGovernanceManager:
    def __init__(self, path) -> None:
        self._manager = DatabaseManager(f"sqlite+pysqlite:///{path}")

    def start(self) -> None:
        self._manager.start()
        Base.metadata.create_all(self._manager.engine)
        with self._manager.session_factory() as session:
            session.add_all(
                [Role(name=role, description=role.value) for role in RoleName]
            )
            session.commit()

    def require_ready(self):
        return self._manager.require_ready()

    def close(self) -> None:
        self._manager.close()


def _no_corpus(_settings):
    raise RuntimeInitializationError(
        "governance_test_has_no_corpus",
        "The governance API test intentionally has no corpus runtime.",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def deterministic_threadpool(monkeypatch) -> None:
    async def dispatch(operation, *args, **kwargs):
        return operation(*args, **kwargs)

    monkeypatch.setattr(auth_routes_module, "run_in_threadpool", dispatch)
    monkeypatch.setattr(governance_routes_module, "run_in_threadpool", dispatch)
    monkeypatch.setattr(security_runtime_module, "run_in_threadpool", dispatch)


def _application(tmp_path):
    manager = _SQLiteGovernanceManager(tmp_path / "governance-api.sqlite")
    settings = Settings(
        magicforge_mode=MagicForgeMode.BOOTSTRAP,
        database_url="postgresql+psycopg://test:test@database/magicforge",
        auth_cookie_secure=False,
        auth_allowed_origins=("http://testserver",),
        bootstrap_allow_anonymous_reads=False,
    )
    return (
        create_app(
            settings=settings,
            runtime_builder=_no_corpus,
            database_manager_factory=lambda _url, _timeout: manager,
        ),
        manager,
    )


def _provision(application) -> None:
    services = application.state.security_services
    services.admin.bootstrap_initial_admin(
        username="ArchiveAdmin",
        password=PASSWORDS["admin"],
    )
    admin = services.auth.login(
        identifier="ArchiveAdmin",
        password=PASSWORDS["admin"],
        transport=SessionTransport.BEARER,
    )
    for username, role in (
        ("OperatorOne", RoleName.OPERATOR),
        ("ReviewerOne", RoleName.REVIEWER),
        ("ReaderOne", RoleName.READER),
    ):
        services.admin.create_user(
            admin.actor,
            username=username,
            password=PASSWORDS[role.value],
            roles=[role],
            reason=f"Provision the independent {role.value} test actor.",
        )


async def _login(client: httpx.AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/auth/login",
        json={"identifier": username, "password": password, "transport": "bearer"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _source_payload(content: str) -> dict:
    digest = sha256(content.encode("utf-8")).hexdigest()
    return {
        "citation": {
            "title": "Attention and an unexpected event",
            "authors": ["A. Researcher"],
            "year": 2020,
            "venue": "Journal of Test Cognition",
            "doi": "10.1000/magicforge.test",
            "url": "https://example.test/attention-paper",
            "peer_review_status": "peer_reviewed",
            "provenance": [
                {
                    "provider": "exa",
                    "tool_name": "exa_search",
                    "query": "magic attention cognition",
                }
            ],
        },
        "source_category": "academic",
        "source_type": "journal_article",
        "knowledge_origin": "scientific_evidence",
        "content": content,
        "content_access": "full_text",
        "access": {
            "access_method": "publisher full text",
            "redistribution_allowed": True,
        },
        "requested_extraction_permission": "full_text",
        "requested_storage_permission": "derived_with_short_excerpt",
        "sensitive_information_level": "controlled",
        "citation_verification": [
            {
                "scope": "metadata",
                "method": "doi_resolver",
                "resolver_result": "matched",
                "verified_identifier": "10.1000/magicforge.test",
                "checked_locator": "https://doi.org/10.1000/magicforge.test",
                "resolver_name": "Crossref",
            },
            {
                "scope": "full_text",
                "method": "content_checksum",
                "resolver_result": "matched",
                "verified_identifier": digest,
                "checked_locator": "https://example.test/attention-paper",
                "resolver_name": "local-sha256",
            },
        ],
    }


def _confidence(reviewer: str) -> dict:
    def dimension(reason: str) -> dict:
        return {"score": 1.0, "reason": reason}

    return {
        "provenance_quality": dimension("Exact immutable source version."),
        "method_rigor": dimension("Controlled experimental method."),
        "claim_directness": dimension("Direct result statement."),
        "consistency": dimension("Contradiction review completed."),
        "magic_applicability": dimension("Relevant to attention management."),
        "assessed_by": reviewer,
    }


@pytest.mark.anyio
async def test_authenticated_source_claim_and_evidence_api(tmp_path) -> None:
    application, manager = _application(tmp_path)
    content = (
        "Participants may fail to notice an unexpected event when their "
        "attention is occupied."
    )
    async with application.router.lifespan_context(application):
        _provision(application)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            operator = await _login(
                client, "OperatorOne", PASSWORDS["operator"]
            )
            reviewer = await _login(
                client, "ReviewerOne", PASSWORDS["reviewer"]
            )
            reader = await _login(client, "ReaderOne", PASSWORDS["reader"])

            missing_key = await client.post(
                "/sources",
                headers=_headers(operator),
                json=_source_payload(content),
            )
            assert missing_key.status_code == 422

            blank_key = await client.post(
                "/sources",
                headers=_headers(operator, "   "),
                json=_source_payload(content),
            )
            assert blank_key.status_code == 422
            assert blank_key.json()["detail"]["code"] == "invalid_idempotency_key"

            cookie_login = await client.post(
                "/auth/login",
                headers={"Origin": "http://testserver"},
                json={
                    "identifier": "OperatorOne",
                    "password": PASSWORDS["operator"],
                    "transport": "cookie",
                },
            )
            assert cookie_login.status_code == 200
            csrf_rejected = await client.post(
                "/sources",
                headers={
                    "Idempotency-Key": "cookie-without-csrf",
                    "Origin": "http://testserver",
                },
                json=_source_payload(content),
            )
            assert csrf_rejected.status_code == 403
            assert csrf_rejected.json()["detail"]["code"] == "csrf_validation_failed"
            client.cookies.clear()

            forbidden_submit = await client.post(
                "/sources",
                headers=_headers(reviewer, "reviewer-cannot-submit"),
                json=_source_payload(content),
            )
            assert forbidden_submit.status_code == 403

            created = await client.post(
                "/sources",
                headers=_headers(operator, "source-register-1"),
                json=_source_payload(content),
            )
            assert created.status_code == 201, created.text
            source = created.json()
            source_id = source["id"]
            version_id = source["versions"][0]["id"]
            permission_request_id = source["versions"][0][
                "latest_permission_request_id"
            ]
            assert permission_request_id
            assert source["versions"][0]["citation_status"] == "unverified"
            assert "content" not in source["versions"][0]

            source_queue = await client.get(
                "/review/sources?status=submitted", headers=_headers(reviewer)
            )
            assert source_queue.status_code == 200, source_queue.text
            queue_item = source_queue.json()["items"][0]
            assert queue_item["source_id"] == source_id
            assert queue_item["source_version_id"] == version_id
            assert queue_item["latest_permission_request_id"] == permission_request_id
            assert queue_item["title"] == "Attention and an unexpected event"
            assert queue_item["status"] == "submitted"
            assert "content" not in queue_item
            reader_source_queue = await client.get(
                "/review/sources", headers=_headers(reader)
            )
            assert reader_source_queue.status_code == 403

            source_detail = await client.get(
                f"/sources/{source_id}", headers=_headers(reviewer)
            )
            assert source_detail.status_code == 200, source_detail.text
            detail_version = source_detail.json()["versions"][0]
            assert detail_version["latest_permission_request_id"] == permission_request_id
            assert detail_version["latest_permission_request"]["id"] == permission_request_id
            assert len(detail_version["permission_requests"]) == 1
            assert detail_version["content"] == content
            assert detail_version["citation"]["url"] == (
                "https://example.test/attention-paper"
            )
            assert all(
                not item["trusted_for_status"]
                for item in detail_version["citation_verification"]
            )
            reader_source = await client.get(
                f"/sources/{source_id}", headers=_headers(reader)
            )
            assert reader_source.status_code == 403
            operator_source = await client.get(
                f"/sources/{source_id}", headers=_headers(operator)
            )
            assert operator_source.status_code == 403

            replay = await client.post(
                "/sources",
                headers=_headers(operator, "source-register-1"),
                json=_source_payload(content),
            )
            assert replay.status_code == 201
            assert replay.json() == source

            conflicting_payload = _source_payload(content + " Different immutable text.")
            conflict = await client.post(
                "/sources",
                headers=_headers(operator, "source-register-1"),
                json=conflicting_payload,
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == "idempotency_conflict"

            permission_payload = {
                "source_version_id": version_id,
                "requested_extraction_permission": "full_text",
                "requested_storage_permission": "derived_with_short_excerpt",
                "requested_scope_locators": [],
                "rights_basis": "The publisher rights record was re-checked.",
                "rights_evidence": ["https://example.test/rights-record"],
                "reason": "Append independently auditable rights evidence.",
            }
            reviewer_cannot_amend = await client.post(
                (
                    f"/sources/{source_id}/versions/{version_id}/"
                    "permission-requests"
                ),
                headers=_headers(reviewer, "reviewer-permission-forbidden"),
                json=permission_payload,
            )
            assert reviewer_cannot_amend.status_code == 403

            cookie_login = await client.post(
                "/auth/login",
                headers={"Origin": "http://testserver"},
                json={
                    "identifier": "OperatorOne",
                    "password": PASSWORDS["operator"],
                    "transport": "cookie",
                },
            )
            assert cookie_login.status_code == 200
            permission_csrf_rejected = await client.post(
                (
                    f"/sources/{source_id}/versions/{version_id}/"
                    "permission-requests"
                ),
                headers={
                    "Idempotency-Key": "permission-cookie-without-csrf",
                    "Origin": "http://testserver",
                },
                json=permission_payload,
            )
            assert permission_csrf_rejected.status_code == 403
            assert permission_csrf_rejected.json()["detail"]["code"] == (
                "csrf_validation_failed"
            )
            client.cookies.clear()

            permission_amendment = await client.post(
                (
                    f"/sources/{source_id}/versions/{version_id}/"
                    "permission-requests"
                ),
                headers=_headers(operator, "permission-amendment-1"),
                json=permission_payload,
            )
            assert permission_amendment.status_code == 201, permission_amendment.text
            permission_request = permission_amendment.json()
            assert permission_request["sequence"] == 2
            assert permission_request["supersedes_request_id"] == permission_request_id
            permission_replay = await client.post(
                (
                    f"/sources/{source_id}/versions/{version_id}/"
                    "permission-requests"
                ),
                headers=_headers(operator, "permission-amendment-1"),
                json=permission_payload,
            )
            assert permission_replay.status_code == 201
            assert permission_replay.json() == permission_request
            latest_permission_request_id = permission_request["id"]
            amended_detail = await client.get(
                f"/sources/{source_id}", headers=_headers(reviewer)
            )
            amended_version = amended_detail.json()["versions"][0]
            assert amended_version["latest_permission_request_id"] == (
                latest_permission_request_id
            )
            assert [
                item["sequence"]
                for item in amended_version["permission_requests"]
            ] == [1, 2]

            unchanged_payload = {
                **permission_payload,
                "reason": "Changing the reason alone is not material.",
            }
            unchanged_permission = await client.post(
                (
                    f"/sources/{source_id}/versions/{version_id}/"
                    "permission-requests"
                ),
                headers=_headers(operator, "permission-amendment-unchanged"),
                json=unchanged_payload,
            )
            assert unchanged_permission.status_code == 409
            assert unchanged_permission.json()["detail"]["code"] == (
                "permission_request_unchanged"
            )

            stale_review = await client.post(
                f"/sources/{source_id}/review",
                headers=_headers(reviewer, "stale-permission-review"),
                json={
                    "source_version_id": version_id,
                    "source_permission_request_id": permission_request_id,
                    "decision": "reject",
                    "reason": "This request is no longer current.",
                },
            )
            assert stale_review.status_code == 409
            assert stale_review.json()["detail"]["code"] == (
                "stale_source_permission_request"
            )
            permission_request_id = latest_permission_request_id

            operator_cannot_review = await client.post(
                f"/sources/{source_id}/review",
                headers=_headers(operator, "operator-review-forbidden"),
                json={
                    "source_version_id": version_id,
                    "source_permission_request_id": permission_request_id,
                    "decision": "approve",
                    "reason": "This must be rejected by RBAC.",
                    "claim_eligibility": "eligible",
                    "extraction_permission": "full_text",
                    "storage_permission": "derived_with_short_excerpt",
                    "sensitive_information_level": "controlled",
                    "contradicting_evidence_checked": "not_applicable",
                    "citation_verification": _source_payload(content)[
                        "citation_verification"
                    ],
                },
            )
            assert operator_cannot_review.status_code == 403

            blank_source_reason = await client.post(
                f"/sources/{source_id}/review",
                headers=_headers(reviewer, "blank-source-review-reason"),
                json={
                    "source_version_id": version_id,
                    "source_permission_request_id": permission_request_id,
                    "decision": "reject",
                    "reason": "   ",
                },
            )
            assert blank_source_reason.status_code == 422
            session_factory = manager.require_ready()
            with session_factory() as session:
                assert session.scalar(
                    select(SourceReviewDecision).where(
                        SourceReviewDecision.source_version_id == UUID(version_id)
                    )
                ) is None
                assert session.scalar(
                    select(AuditEvent).where(
                        AuditEvent.object_id == version_id,
                        AuditEvent.event_type.in_({"source_approved", "source_rejected"}),
                    )
                ) is None
                assert session.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.idempotency_key
                        == "blank-source-review-reason"
                    )
                ) is None

            reviewed = await client.post(
                f"/sources/{source_id}/review",
                headers=_headers(reviewer, "source-review-1"),
                json={
                    "source_version_id": version_id,
                    "source_permission_request_id": permission_request_id,
                    "decision": "approve",
                    "reason": "Metadata and exact content were independently checked.",
                    "claim_eligibility": "eligible",
                    "extraction_permission": "full_text",
                    "storage_permission": "derived_with_short_excerpt",
                    "sensitive_information_level": "controlled",
                    "contradicting_evidence_checked": "not_applicable",
                    "citation_verification": _source_payload(content)[
                        "citation_verification"
                    ],
                },
            )
            assert reviewed.status_code == 200, reviewed.text
            assert reviewed.json()["versions"][0]["status"] == "approved"
            assert (
                reviewed.json()["versions"][0]["citation_status"]
                == "full_text_verified"
            )
            reviewed_detail = await client.get(
                f"/sources/{source_id}", headers=_headers(reviewer)
            )
            trusted_evidence = [
                item
                for item in reviewed_detail.json()["versions"][0][
                    "citation_verification"
                ]
                if item["trusted_for_status"]
            ]
            assert trusted_evidence
            assert {item["review_actor"] for item in trusted_evidence} == {
                "ReviewerOne"
            }

            claim_created = await client.post(
                "/claims",
                headers=_headers(operator, "claim-submit-1"),
                json={
                    "source_version_id": version_id,
                    "claim": content,
                    "claim_role": "result",
                    "proposed_evidence_class": "controlled_experiment",
                    "applicable_domain": ["theory"],
                    "ontology_paths": [
                        "psychology.attention.inattentional_blindness"
                    ],
                    "locator": {
                        "media_type": "web",
                        "source_locator": "https://example.test/attention-paper",
                    },
                    "evidence_excerpt": content,
                    "proposed_limitations": ["Single experimental context."],
                    "extraction_confidence": 0.9,
                    "extraction_provenance": {
                        "producer": "glm",
                        "extractor": "research-extractor",
                        "extraction_schema_version": "extraction-0.2",
                        "run_id": "run-api-1",
                        "llm_provider": "GLM",
                        "model": "glm-4.7",
                    },
                },
            )
            assert claim_created.status_code == 201, claim_created.text
            claim = claim_created.json()
            claim_id = claim["id"]
            assert claim["status"] == "submitted"
            assert "extraction_provenance" not in claim

            claim_detail = await client.get(
                f"/claims/{claim_id}", headers=_headers(reviewer)
            )
            assert claim_detail.status_code == 200, claim_detail.text
            assert claim_detail.json()["evidence_excerpt"] == content
            assert claim_detail.json()["extraction_provenance"]["llm_provider"] == "GLM"
            source_context = claim_detail.json()["source_context"]
            assert source_context["source_id"] == source_id
            assert source_context["source_version_id"] == version_id
            assert source_context["title"] == "Attention and an unexpected event"
            assert source_context["citation"]["url"] == (
                "https://example.test/attention-paper"
            )
            assert source_context["access"]["access_method"] == (
                "publisher full text"
            )
            assert source_context["content"] == content
            assert source_context["sensitivity"] == "controlled"
            reader_claim = await client.get(
                f"/claims/{claim_id}", headers=_headers(reader)
            )
            assert reader_claim.status_code == 403

            blank_claim_reason = await client.post(
                f"/claims/{claim_id}/review",
                headers=_headers(reviewer, "blank-claim-review-reason"),
                json={
                    "decision": "reject",
                    "reason": "   ",
                },
            )
            assert blank_claim_reason.status_code == 422
            with session_factory() as session:
                assert session.scalar(
                    select(ClaimReviewDecision).where(
                        ClaimReviewDecision.claim_candidate_id == UUID(claim_id)
                    )
                ) is None
                assert session.scalar(
                    select(AuditEvent).where(
                        AuditEvent.object_id == claim_id,
                        AuditEvent.event_type.in_({"claim_approved", "claim_rejected"}),
                    )
                ) is None
                assert session.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.idempotency_key
                        == "blank-claim-review-reason"
                    )
                ) is None

            queue = await client.get(
                "/review/claims", headers=_headers(reviewer)
            )
            assert queue.status_code == 200
            assert [item["id"] for item in queue.json()["items"]] == [claim_id]

            low_confidence = _confidence("ReviewerOne")
            for dimension in (
                "provenance_quality",
                "method_rigor",
                "claim_directness",
                "consistency",
                "magic_applicability",
            ):
                low_confidence[dimension]["score"] = 0.0
            insufficient = await client.post(
                f"/claims/{claim_id}/review",
                headers=_headers(reviewer, "claim-review-insufficient"),
                json={
                    "decision": "approve",
                    "reason": "This confidence is intentionally insufficient.",
                    "confidence": low_confidence,
                    "claim_eligibility": "eligible",
                    "storage_permission": "derived_with_short_excerpt",
                    "sensitive_information_level": "controlled",
                    "contradiction_status": "none_found",
                    "contradicting_evidence_checked": "checked_none_found",
                    "limitations": ["Single experimental context."],
                    "secret_exposure_level": "general_principle",
                },
            )
            assert insufficient.status_code == 422
            assert insufficient.json()["detail"]["code"] == "confidence_insufficient"

            approved = await client.post(
                f"/claims/{claim_id}/review",
                headers=_headers(reviewer, "claim-review-1"),
                json={
                    "decision": "approve",
                    "reason": "The atomic claim is directly supported by the locator.",
                    "confidence": _confidence("ReviewerOne"),
                    "claim_eligibility": "eligible",
                    "storage_permission": "derived_with_short_excerpt",
                    "sensitive_information_level": "controlled",
                    "contradiction_status": "none_found",
                    "contradicting_evidence_checked": "checked_none_found",
                    "limitations": ["Single experimental context."],
                    "secret_exposure_level": "general_principle",
                },
            )
            assert approved.status_code == 200, approved.text
            result = approved.json()
            assert result["claim"]["status"] == "approved"
            evidence_card_id = result["evidence_card_id"]
            evidence_version_id = result["evidence_version_id"]

            evidence = await client.get(
                f"/evidence/{evidence_card_id}/versions",
                headers=_headers(reader),
            )
            assert evidence.status_code == 200, evidence.text
            view = evidence.json()["items"][0]
            assert view["claim"] == content
            assert view["evidence_excerpt"] == content
            assert "reviewer" not in view
            assert "approval_reason" not in view

            exact_evidence = await client.get(
                f"/evidence/{evidence_version_id}/versions",
                headers=_headers(reader),
            )
            assert exact_evidence.status_code == 200, exact_evidence.text
            assert [
                item["evidence_version_id"]
                for item in exact_evidence.json()["items"]
            ] == [evidence_version_id]

            reader_queue = await client.get(
                "/review/claims", headers=_headers(reader)
            )
            assert reader_queue.status_code == 403
