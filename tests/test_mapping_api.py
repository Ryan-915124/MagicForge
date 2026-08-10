"""HTTP authorization and response contract for Production Mapping."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

import app.mapping_routes as mapping_routes_module
from knowledge.models import EntityType
from persistence.models import SessionTransport
from research.review.mapping_models import CanonicalResolution
from test_governance_api import (
    PASSWORDS,
    _application,
    _headers,
    _login,
    _provision,
    anyio_backend,
    deterministic_threadpool,
)
from test_governance_workflow import (
    _claim_review_command,
    _register_and_approve_source,
    _submit_claim,
)
from test_mapping_workflow import _entity_command


@pytest.fixture(autouse=True)
def deterministic_mapping_threadpool(monkeypatch) -> None:
    async def dispatch(operation, *args, **kwargs):
        return operation(*args, **kwargs)

    monkeypatch.setattr(mapping_routes_module, "run_in_threadpool", dispatch)


@pytest.mark.anyio
async def test_mapping_api_requires_distinct_operator_and_reviewer(tmp_path) -> None:
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
            harness, slug="mapping-api"
        )
        claim = _submit_claim(
            harness,
            source_version_id,
            key="mapping-api-claim",
        )
        evidence = harness.service.review_claim(
            reviewer_actor,
            claim.id,
            _claim_review_command(reviewer_actor.username),
            idempotency_key="mapping-api-claim-review",
        )
        command = _entity_command(
            evidence.evidence_version_id,
            entity_type=EntityType.PSYCHOLOGY_PRINCIPLE,
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            operator = await _login(client, "OperatorOne", PASSWORDS["operator"])
            reviewer = await _login(client, "ReviewerOne", PASSWORDS["reviewer"])

            anonymous = await client.post(
                "/mappings/entities",
                headers={"Idempotency-Key": "anonymous-mapping"},
                json=command.model_dump(mode="json"),
            )
            assert anonymous.status_code == 401

            wrong_submitter = await client.post(
                "/mappings/entities",
                headers=_headers(reviewer, "reviewer-mapping-submit"),
                json=command.model_dump(mode="json"),
            )
            assert wrong_submitter.status_code == 403

            submitted = await client.post(
                "/mappings/entities",
                headers=_headers(operator, "operator-mapping-submit"),
                json=command.model_dump(mode="json"),
            )
            assert submitted.status_code == 201, submitted.text
            mapping_id = submitted.json()["id"]

            operator_queue = await client.get(
                "/review/mappings", headers=_headers(operator)
            )
            assert operator_queue.status_code == 403
            queue = await client.get(
                "/review/mappings", headers=_headers(reviewer)
            )
            assert queue.status_code == 200
            assert queue.json()["items"][0]["id"] == mapping_id

            review_payload = {
                "decision": "approve",
                "reason": "The exact evidence version and ontology placement were checked.",
                "confidence": _claim_review_command(
                    reviewer_actor.username
                ).confidence.model_dump(mode="json"),
                "canonical_resolution": CanonicalResolution.CREATE.value,
            }
            wrong_reviewer = await client.post(
                f"/mappings/{mapping_id}/review",
                headers=_headers(operator, "operator-mapping-review"),
                json=review_payload,
            )
            assert wrong_reviewer.status_code == 403

            approved = await client.post(
                f"/mappings/{mapping_id}/review",
                headers=_headers(reviewer, "reviewer-mapping-review"),
                json=review_payload,
            )
            assert approved.status_code == 200, approved.text
            version_id = approved.json()["knowledge_node_version_id"]
            version = await client.get(
                f"/knowledge/versions/{version_id}",
                headers=_headers(reviewer),
            )
            assert version.status_code == 200
            assert version.json()["node"]["entity"]["name"] == (
                "Inattentional Blindness"
            )

            canonical = await client.get(
                "/knowledge/entities",
                params={
                    "query": "inattentional",
                    "entity_type": EntityType.PSYCHOLOGY_PRINCIPLE.value,
                    "status": "active",
                },
                headers=_headers(reviewer),
            )
            assert canonical.status_code == 200, canonical.text
            canonical_item = canonical.json()["items"][0]
            assert canonical_item["id"] == version.json()["entity_id"]
            assert canonical_item["canonical_name"] == "Inattentional Blindness"
            assert canonical_item["latest_knowledge_node_version_id"] == version_id
            assert canonical_item["status"] == "active"

            operator_canonical = await client.get(
                "/knowledge/entities", headers=_headers(operator)
            )
            assert operator_canonical.status_code == 403
    manager.close()
