"""Authenticated API smoke test for manifest and corpus governance routes."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

import app.auth_routes as auth_routes_module
import app.governance_routes as governance_routes_module
import app.security_runtime as security_runtime_module
import app.storage_routes as storage_routes_module
from persistence.models import SessionTransport
from research.review.storage_workflow_service import ProductionStorageWorkflowService
from test_governance_api import (
    PASSWORDS,
    _application,
    _headers,
    _login,
    _provision,
    anyio_backend,
)
from test_governance_workflow import NOW
from test_governance_workflow import (
    CLAIM_TEXT,
    _claim_command,
    _claim_review_command,
    _register_and_approve_source,
)
from test_storage_workflow import FakeWriter, StaticResolver


@pytest.fixture(autouse=True)
def deterministic_storage_threadpool(monkeypatch) -> None:
    async def dispatch(operation, *args, **kwargs):
        return operation(*args, **kwargs)

    for module in (
        auth_routes_module,
        governance_routes_module,
        security_runtime_module,
        storage_routes_module,
    ):
        monkeypatch.setattr(module, "run_in_threadpool", dispatch)


@pytest.mark.anyio
async def test_storage_api_requires_operator_and_exposes_active_corpus(tmp_path) -> None:
    application, manager = _application(tmp_path)
    async with application.router.lifespan_context(application):
        _provision(application)
        application.state.storage_workflow_service = ProductionStorageWorkflowService(
            manager.require_ready(),
            artifact_resolver=StaticResolver(),
            writer=FakeWriter(),
            allow_temporary_qdrant_writes=True,
            persistent_production_write_approved=False,
            vector_size=3,
            vector_distance="cosine",
            clock=lambda: NOW,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            operator = await _login(client, "OperatorOne", PASSWORDS["operator"])
            reviewer = await _login(client, "ReviewerOne", PASSWORDS["reviewer"])
            corpus_id = str(uuid4())
            payload = {
                "corpus_id": corpus_id,
                "collection_name": f"magicforge_cp4_test_{uuid4().hex}",
                "evidence_version_ids": [str(uuid4())],
                "authorized_sensitive_levels": ["public", "controlled"],
            }

            anonymous = await client.post(
                "/storage/manifests",
                headers={"Idempotency-Key": "anonymous-build"},
                json=payload,
            )
            assert anonymous.status_code == 401

            built = await client.post(
                "/storage/manifests",
                headers=_headers(operator, "operator-build"),
                json=payload,
            )
            assert built.status_code == 201, built.text
            manifest_id = built.json()["id"]
            assert built.json()["status"] == "pending"

            manifests = await client.get(
                "/storage/manifests",
                params={"status": "pending", "corpus_id": corpus_id},
                headers=_headers(operator),
            )
            assert manifests.status_code == 200, manifests.text
            manifest_summary = manifests.json()["items"][0]
            assert manifest_summary["id"] == manifest_id
            assert manifest_summary["corpus_id"] == corpus_id
            assert manifest_summary["status"] == "pending"
            assert manifest_summary["expected_point_count"] == 1
            assert "items" not in manifest_summary
            reviewer_manifests = await client.get(
                "/storage/manifests", headers=_headers(reviewer)
            )
            assert reviewer_manifests.status_code == 403

            authorized = await client.post(
                f"/storage/manifests/{manifest_id}/authorize",
                headers=_headers(operator, "operator-authorize"),
                json={"reason": "Authorize the exact isolated API test manifest."},
            )
            assert authorized.status_code == 200, authorized.text
            assert authorized.json()["status"] == "authorized"

            ingested = await client.post(
                f"/storage/manifests/{manifest_id}/ingest",
                headers=_headers(operator, "operator-ingest"),
                json={"reason": "Ingest only into the isolated API test writer."},
            )
            assert ingested.status_code == 200, ingested.text
            assert ingested.json()["point_count"] == 1

            activated = await client.post(
                f"/corpora/{corpus_id}/activate",
                headers=_headers(operator, "operator-activate"),
                json={
                    "runtime_scope": "production",
                    "reason": "Activate the receipt-backed isolated API test corpus.",
                },
            )
            assert activated.status_code == 200, activated.text
            assert activated.json()["activation_state"] == "active"

            active = await client.get(
                "/corpora/active", headers=_headers(operator)
            )
            assert active.status_code == 200
            assert active.json()["corpus_id"] == corpus_id
    manager.close()


@pytest.mark.anyio
async def test_eligible_artifacts_expose_exact_current_approved_rows(tmp_path) -> None:
    application, manager = _application(tmp_path)
    async with application.router.lifespan_context(application):
        _provision(application)
        services = application.state.security_services
        operator_actor = services.auth.login(
            identifier="OperatorOne",
            password=PASSWORDS["operator"],
            transport=SessionTransport.BEARER,
        ).actor
        reviewer_actor = services.auth.login(
            identifier="ReviewerOne",
            password=PASSWORDS["reviewer"],
            transport=SessionTransport.BEARER,
        ).actor
        harness = SimpleNamespace(
            service=application.state.governance_service,
            operator=operator_actor,
            reviewer=reviewer_actor,
        )
        _source_id, source_version_id = _register_and_approve_source(
            harness, slug="eligible-artifact-api"
        )
        claim = harness.service.submit_claim(
            operator_actor,
            _claim_command(
                source_version_id,
                claim=(
                    "Participants may overlook an unexpected action in a "
                    "demonstration."
                ),
            ).model_copy(update={"evidence_excerpt": CLAIM_TEXT}),
            idempotency_key="eligible-artifact-api-claim",
        )
        evidence = harness.service.review_claim(
            reviewer_actor,
            claim.id,
            _claim_review_command(reviewer_actor.username),
            idempotency_key="eligible-artifact-api-review",
        )
        assert evidence.evidence_version_id is not None
        storage_service = ProductionStorageWorkflowService(
            manager.require_ready(),
            writer=FakeWriter(),
            allow_temporary_qdrant_writes=True,
            persistent_production_write_approved=False,
            vector_size=3,
            vector_distance="cosine",
            clock=lambda: NOW,
        )
        application.state.storage_workflow_service = storage_service

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            operator = await _login(client, "OperatorOne", PASSWORDS["operator"])
            reviewer = await _login(client, "ReviewerOne", PASSWORDS["reviewer"])
            eligible = await client.get(
                "/storage/eligible-artifacts",
                params={"artifact_type": "evidence_card"},
                headers=_headers(operator),
            )
            assert eligible.status_code == 200, eligible.text
            item = eligible.json()["items"][0]
            assert item["artifact_type"] == "evidence_card"
            assert item["artifact_row_id"] == str(evidence.evidence_version_id)
            assert item["artifact_version"] == 1
            assert item["supporting_evidence_version_ids"] == [
                str(evidence.evidence_version_id)
            ]
            assert item["sensitivity"] == "controlled"
            assert item["payload_checksum"]
            assert item["subject"]

            catalogue_bound = await client.get(
                "/storage/eligible-artifacts",
                params={"offset": 9_901},
                headers=_headers(operator),
            )
            assert catalogue_bound.status_code == 422

            reviewer_denied = await client.get(
                "/storage/eligible-artifacts", headers=_headers(reviewer)
            )
            assert reviewer_denied.status_code == 403
    manager.close()
