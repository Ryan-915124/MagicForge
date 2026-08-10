"""HTTP contract tests for governed Production extraction jobs.

The API must expose only content-free job/readiness metadata.  These tests do
not execute GLM extraction and do not create Evidence Cards or Qdrant points.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.exc import SQLAlchemyError

import app.auth_routes as auth_routes_module
import app.extraction_routes as extraction_routes_module
import app.governance_routes as governance_routes_module
import app.security_runtime as security_runtime_module
from research.extraction.production_jobs import ProductionExtractionServiceError
from test_governance_api import (
    PASSWORDS,
    _application,
    _headers,
    _login,
    _provision,
    anyio_backend,
)
from test_governance_workflow import _register_and_approve_source


@pytest.fixture(autouse=True)
def deterministic_extraction_threadpool(monkeypatch) -> None:
    async def dispatch(operation, *args, **kwargs):
        return operation(*args, **kwargs)

    for module in (
        auth_routes_module,
        extraction_routes_module,
        governance_routes_module,
        security_runtime_module,
    ):
        monkeypatch.setattr(module, "run_in_threadpool", dispatch)


def _approve_exact_source(application, *, slug: str = "extraction-api"):
    services = application.state.security_services
    operator = services.auth.login(
        identifier="OperatorOne",
        password=PASSWORDS["operator"],
        transport="bearer",
    ).actor
    reviewer = services.auth.login(
        identifier="ReviewerOne",
        password=PASSWORDS["reviewer"],
        transport="bearer",
    ).actor
    harness = SimpleNamespace(
        service=application.state.governance_service,
        operator=operator,
        reviewer=reviewer,
    )
    return _register_and_approve_source(harness, slug=slug)


@pytest.mark.anyio
async def test_extraction_api_operator_rbac_csrf_and_content_free_views(
    tmp_path,
) -> None:
    application, manager = _application(tmp_path)
    async with application.router.lifespan_context(application):
        _provision(application)
        source_id, source_version_id = _approve_exact_source(application)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            anonymous = await client.get("/research/extractions/eligible")
            assert anonymous.status_code == 401
            assert anonymous.json()["detail"]["code"] == "unauthenticated"

            operator = await _login(client, "OperatorOne", PASSWORDS["operator"])
            reviewer = await _login(client, "ReviewerOne", PASSWORDS["reviewer"])

            reviewer_eligible = await client.get(
                "/research/extractions/eligible", headers=_headers(reviewer)
            )
            assert reviewer_eligible.status_code == 403
            assert reviewer_eligible.json()["detail"]["code"] == "forbidden"

            eligible = await client.get(
                "/research/extractions/eligible",
                params={"limit": 10, "offset": 0},
                headers=_headers(operator),
            )
            assert eligible.status_code == 200, eligible.text
            eligible_payload = eligible.json()
            assert eligible_payload["limit"] == 10
            assert eligible_payload["offset"] == 0
            assert len(eligible_payload["items"]) == 1
            ready = eligible_payload["items"][0]
            assert ready["source_id"] == str(source_id)
            assert ready["source_version_id"] == str(source_version_id)
            assert ready["citation_status"] == "full_text_verified"
            assert ready["extraction_permission"] == "full_text"
            assert "content" not in ready
            assert "content_text" not in ready

            cookie_login = await client.post(
                "/auth/login",
                headers={"Origin": "http://testserver"},
                json={
                    "identifier": "OperatorOne",
                    "password": PASSWORDS["operator"],
                    "transport": "cookie",
                },
            )
            assert cookie_login.status_code == 200, cookie_login.text
            csrf_cookie = client.cookies.get("magicforge_csrf")
            assert csrf_cookie

            csrf_rejected = await client.post(
                "/research/extractions",
                headers={"Origin": "http://testserver"},
                json={"source_version_id": str(source_version_id)},
            )
            assert csrf_rejected.status_code == 403
            assert csrf_rejected.json()["detail"]["code"] == (
                "csrf_validation_failed"
            )

            queued = await client.post(
                "/research/extractions",
                headers={
                    "Origin": "http://testserver",
                    "X-CSRF-Token": csrf_cookie,
                },
                json={"source_version_id": str(source_version_id)},
            )
            assert queued.status_code == 201, queued.text
            queued_payload = queued.json()
            assert queued_payload["created"] is True
            job = queued_payload["job"]
            assert job["source_version_id"] == str(source_version_id)
            assert job["provider"] == "GLM"
            assert job["status"] == "queued"
            assert job["attempt_count"] == 0
            assert job["attempts"] == []
            assert "content" not in job
            assert "source_text" not in job

            run_without_csrf = await client.post(
                f"/research/extractions/{job['id']}/run",
                headers={"Origin": "http://testserver"},
            )
            assert run_without_csrf.status_code == 403

            run_disabled = await client.post(
                f"/research/extractions/{job['id']}/run",
                headers={
                    "Origin": "http://testserver",
                    "X-CSRF-Token": csrf_cookie,
                },
            )
            assert run_disabled.status_code == 409
            assert run_disabled.json()["detail"]["code"] == (
                "production_glm_extraction_disabled"
            )

            client.cookies.clear()
            replay = await client.post(
                "/research/extractions",
                headers=_headers(operator),
                json={"source_version_id": str(source_version_id)},
            )
            assert replay.status_code == 201, replay.text
            assert replay.json()["created"] is False
            assert replay.json()["job"]["id"] == job["id"]

            reviewer_queue = await client.post(
                "/research/extractions",
                headers=_headers(reviewer),
                json={"source_version_id": str(source_version_id)},
            )
            assert reviewer_queue.status_code == 403

            jobs = await client.get(
                "/research/extractions",
                params={"status": "queued", "limit": 25, "offset": 0},
                headers=_headers(operator),
            )
            assert jobs.status_code == 200, jobs.text
            assert jobs.json()["limit"] == 25
            assert jobs.json()["offset"] == 0
            assert [item["id"] for item in jobs.json()["items"]] == [job["id"]]

            detail = await client.get(
                f"/research/extractions/{job['id']}", headers=_headers(operator)
            )
            assert detail.status_code == 200, detail.text
            assert detail.json() == job
    manager.close()


@pytest.mark.anyio
async def test_extraction_api_validation_and_safe_error_mapping(tmp_path) -> None:
    application, manager = _application(tmp_path)
    async with application.router.lifespan_context(application):
        _provision(application)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            operator = await _login(client, "OperatorOne", PASSWORDS["operator"])

            invalid_limit = await client.get(
                "/research/extractions/eligible",
                params={"limit": 0},
                headers=_headers(operator),
            )
            assert invalid_limit.status_code == 422

            invalid_status = await client.get(
                "/research/extractions",
                params={"status": "approved"},
                headers=_headers(operator),
            )
            assert invalid_status.status_code == 422

            unknown_source = await client.post(
                "/research/extractions",
                headers=_headers(operator),
                json={"source_version_id": str(uuid4())},
            )
            assert unknown_source.status_code == 409
            assert unknown_source.json()["detail"] == {
                "code": "source_version_not_found",
                "message": "The Source version is unavailable.",
            }

            unknown_job = await client.get(
                f"/research/extractions/{uuid4()}", headers=_headers(operator)
            )
            assert unknown_job.status_code == 404
            assert unknown_job.json()["detail"] == {
                "code": "extraction_job_not_found",
                "message": "The extraction job is unavailable.",
            }

            class RejectedService:
                def list_eligible_sources(self, *_args, **_kwargs):
                    raise ProductionExtractionServiceError(
                        "safe_test_rejection", "The request was safely rejected."
                    )

            application.state.extraction_service = RejectedService()
            rejected = await client.get(
                "/research/extractions/eligible", headers=_headers(operator)
            )
            assert rejected.status_code == 409
            assert rejected.json()["detail"] == {
                "code": "safe_test_rejection",
                "message": "The request was safely rejected.",
            }

            class BrokenPersistenceService:
                def list_eligible_sources(self, *_args, **_kwargs):
                    raise SQLAlchemyError("private database diagnostics")

            application.state.extraction_service = BrokenPersistenceService()
            unavailable = await client.get(
                "/research/extractions/eligible", headers=_headers(operator)
            )
            assert unavailable.status_code == 503
            assert unavailable.json()["detail"] == {
                "code": "extraction_persistence_unavailable",
                "message": "Production extraction persistence is temporarily unavailable.",
            }
            assert "private database diagnostics" not in unavailable.text

            application.state.extraction_service = None
            not_ready = await client.get(
                "/research/extractions/eligible", headers=_headers(operator)
            )
            assert not_ready.status_code == 503
            assert not_ready.json()["detail"] == {
                "code": "extraction_persistence_not_ready",
                "message": "Production extraction persistence is unavailable.",
            }
    manager.close()
