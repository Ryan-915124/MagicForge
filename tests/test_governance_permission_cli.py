"""Security-boundary tests for the governed Source permission CLI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
import pytest

import security.governance_permission_cli as cli
from research.review.workflow_models import SourceVersionCommand
from security import governance_source_cli as source_cli


OPERATOR_PASSWORD = "a hidden permission operator passphrase"
TOKEN = "server-secret-permission-bearer-token"
SOURCE_CONTENT = "private exact registered Source body"


def _source_submission(
    *,
    marker: str = "one",
    content: str = SOURCE_CONTENT,
) -> source_cli.PreparedSourceSubmission:
    doi = f"10.1000/magicforge.permission.{marker}"
    source_id = uuid5(NAMESPACE_URL, f"magicforge:research:{doi}")
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    payload = {
        "schema_version": "source-workflow-0.1",
        "citation": {
            "title": f"Controlled permission source {marker}",
            "authors": ["A. Researcher"],
            "year": 2024,
            "venue": "Journal of Test Cognition",
            "volume": "1",
            "issue": "2",
            "pages": "3-4",
            "doi": doi,
            "url": f"https://example.test/source/{marker}#fragment",
            "peer_review_status": "unknown",
            "provenance": [
                {
                    "provider": "exa",
                    "tool_name": "web_search_exa",
                    "query": "existing locked source",
                }
            ],
        },
        "source_category": "academic",
        "source_type": "journal_article",
        "knowledge_origin": "scientific_evidence",
        "content": content,
        "content_access": "web_extract",
        "access": {
            "access_method": "Tavily exact-URL extract (basic)",
            "license_name": None,
            "rights_uri": None,
            "permission_notes": "Registration only; review required.",
            "redistribution_allowed": False,
        },
        "requested_extraction_permission": "none",
        "requested_storage_permission": "none",
        "requested_scope_locators": [],
        "sensitive_information_level": "controlled",
        "citation_verification": [],
    }
    command = SourceVersionCommand.model_validate(payload)
    return source_cli.PreparedSourceSubmission(
        file_name=f"{source_id}.json",
        candidate_id=source_id,
        expected_source_id=source_id,
        canonical_key=f"doi:{doi}",
        content_sha256=content_sha256,
        idempotency_key=(
            f"corpus-run-001:source:{source_id}:{content_sha256}"
        ),
        command=command,
        payload=payload,
    )


def _wrapper(submission: source_cli.PreparedSourceSubmission) -> dict:
    return {
        "candidate_id": str(submission.candidate_id),
        "expected_production_source_id": str(submission.expected_source_id),
        "content_sha256": submission.content_sha256,
        "idempotency_key": submission.idempotency_key,
        "payload": dict(submission.payload),
    }


def _fake_batch(
    prepared: cli.PreparedPermissionRequest,
) -> source_cli.OfficialAcquisitionBatch:
    return source_cli.OfficialAcquisitionBatch(
        results_file=Path("/locked/acquisition-results.json"),
        results_sha256=source_cli._OFFICIAL_RESULTS_SHA256,
        source_manifest_sha256=source_cli._OFFICIAL_MANIFEST_SHA256,
        expected_by_file={
            prepared.file_name: source_cli.OfficialPayloadCommitment(
                candidate_id=prepared.candidate_id,
                content_sha256=prepared.content_sha256,
                payload_sha256="b" * 64,
            )
        },
    )


def _patch_preflight(
    monkeypatch: pytest.MonkeyPatch,
    prepared: cli.PreparedPermissionRequest,
) -> None:
    batch = _fake_batch(prepared)
    monkeypatch.setattr(
        cli,
        "_preflight_official_permissions",
        lambda _payload_dir, _results_file: ([prepared], [], batch),
    )


def _arguments(*extra: str) -> list[str]:
    return [
        "request",
        "--operator-identifier",
        "ReleaseOperator",
        "--payload-dir",
        "/locked/source_payloads",
        "--results-file",
        "/locked/acquisition-results.json",
        *extra,
    ]


def _login_response(*, roles: list[str] | None = None) -> dict:
    return {
        "transport": "bearer",
        "access_token": TOKEN,
        "actor": {"roles": roles or ["operator"]},
    }


def _permission_response(
    prepared: cli.PreparedPermissionRequest,
    **overrides: object,
) -> dict:
    command = prepared.command
    result = {
        "id": str(
            uuid5(
                NAMESPACE_URL,
                f"magicforge:test-permission:{prepared.idempotency_key}",
            )
        ),
        "source_version_id": str(prepared.source_version_id),
        "sequence": 2,
        "requested_extraction_permission": "full_text",
        "requested_storage_permission": "derived_knowledge_only",
        "requested_scope_locators": [],
        "rights_basis": command.rights_basis,
        "rights_evidence": command.rights_evidence,
        "reason": command.reason,
        "submitted_by": "ReleaseOperator",
        "actor_role_snapshot": ["operator"],
        "request_checksum": "a" * 64,
        "supersedes_request_id": str(
            uuid5(NAMESPACE_URL, f"magicforge:initial:{prepared.source_id}")
        ),
        "created_at": "2026-08-08T00:00:00Z",
    }
    result.update(overrides)
    return result


def test_http_client_is_proxy_independent_and_does_not_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def client_constructor(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(cli.httpx, "Client", client_constructor)

    assert cli._new_client("http://127.0.0.1:8000") is sentinel
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False


def test_permission_command_is_exact_and_contains_no_source_content() -> None:
    source = _source_submission()
    prepared = cli._prepare_permission_request(source)
    command = prepared.command

    assert prepared.source_version_id == uuid5(
        NAMESPACE_URL,
        f"magicforge:source-version:{source.expected_source_id}:"
        f"{source.content_sha256}",
    )
    assert command.source_version_id == prepared.source_version_id
    assert command.requested_extraction_permission.value == "full_text"
    assert command.requested_storage_permission.value == "derived_knowledge_only"
    assert command.requested_scope_locators == []
    assert "not Source approval" in command.reason
    assert "Independent human" in command.rights_basis
    assert command.rights_evidence == [
        "canonical_url=https://example.test/source/one",
        "doi=10.1000/magicforge.permission.one",
        f"content_sha256={source.content_sha256}",
        "content_access=web_extract",
        "access_method=Tavily exact-URL extract (basic)",
        "redistribution_allowed=false",
    ]
    serialized = json.dumps(command.model_dump(mode="json"))
    assert SOURCE_CONTENT not in serialized
    assert '"content"' not in serialized
    assert cli._RIGHTS_BASIS not in prepared.dry_run_record().values()


def test_default_is_offline_dry_run_without_prompt_or_http(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = cli._prepare_permission_request(_source_submission())
    _patch_preflight(monkeypatch, prepared)
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda _prompt: (_ for _ in ()).throw(
            AssertionError("dry-run must not prompt")
        ),
    )
    monkeypatch.setattr(
        cli,
        "_new_client",
        lambda _url: (_ for _ in ()).throw(
            AssertionError("dry-run must not create a client")
        ),
    )

    assert cli.main(_arguments("--json")) == 0
    output = capsys.readouterr().out
    assert SOURCE_CONTENT not in output
    report = json.loads(output)
    assert report["status"] == "dry_run"
    assert report["permission_requests_sent"] == 0
    assert report["preflight"] == {"status": "passed", "request_count": 1}


def test_official_batch_dry_run_locks_all_29_sources_and_is_offline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    synthetic_official_cli_bundle,
) -> None:
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda _prompt: (_ for _ in ()).throw(
            AssertionError("official dry-run must not prompt")
        ),
    )
    monkeypatch.setattr(
        cli,
        "_new_client",
        lambda _url: (_ for _ in ()).throw(
            AssertionError("official dry-run must not create a client")
        ),
    )

    assert cli.main(
        ["request", "--operator-identifier", "DryRunOperator", "--json"]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["preflight"] == {"status": "passed", "request_count": 29}
    assert report["permission_requests_sent"] == 0
    assert report["batch"] == {
        "run_id": "production-source-acquisition-001",
        "results_sha256": source_cli._OFFICIAL_RESULTS_SHA256,
        "source_manifest_sha256": source_cli._OFFICIAL_MANIFEST_SHA256,
        "payload_count": 29,
    }
    assert len({item["source_version_id"] for item in report["requests"]}) == 29


def test_payload_hash_tampering_fails_before_prompt_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source_submission(content="hash-bound private body")
    wrapper = _wrapper(source)
    payload_dir = tmp_path / "source_payloads"
    payload_dir.mkdir()
    path = payload_dir / source.file_name
    original = json.dumps(wrapper).encode("utf-8")
    path.write_bytes(original)
    batch = source_cli.OfficialAcquisitionBatch(
        results_file=tmp_path / "acquisition-results.json",
        results_sha256=source_cli._OFFICIAL_RESULTS_SHA256,
        source_manifest_sha256=source_cli._OFFICIAL_MANIFEST_SHA256,
        expected_by_file={
            path.name: source_cli.OfficialPayloadCommitment(
                candidate_id=source.candidate_id,
                content_sha256=source.content_sha256,
                payload_sha256=hashlib.sha256(original).hexdigest(),
            )
        },
    )
    monkeypatch.setattr(
        source_cli,
        "_load_official_batch",
        lambda _results, _payloads: batch,
    )
    tampered = dict(wrapper)
    tampered["content_sha256"] = "f" * 64
    path.write_text(json.dumps(tampered), encoding="utf-8")
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda _prompt: (_ for _ in ()).throw(
            AssertionError("failed preflight must not prompt")
        ),
    )
    monkeypatch.setattr(
        cli,
        "_new_client",
        lambda _url: (_ for _ in ()).throw(
            AssertionError("failed preflight must not create a client")
        ),
    )

    assert cli.main(
        [
            "request",
            "--operator-identifier",
            "ReleaseOperator",
            "--payload-dir",
            str(payload_dir),
            "--results-file",
            str(tmp_path / "acquisition-results.json"),
            "--execute",
            "--json",
        ]
    ) == 2
    output = capsys.readouterr().out
    assert "hash-bound private body" not in output
    report = json.loads(output)
    assert report["preflight"]["errors"] == [
        {"file": source.file_name, "code": "payload_file_hash_mismatch"}
    ]
    assert report["permission_requests_sent"] == 0


def test_execute_posts_only_permission_endpoint_with_idempotency_then_logs_out(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = cli._prepare_permission_request(_source_submission())
    _patch_preflight(monkeypatch, prepared)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if request.url.path == "/auth/login":
            assert body == {
                "identifier": "ReleaseOperator",
                "password": OPERATOR_PASSWORD,
                "transport": "bearer",
                "device_label": "governance-permission-cli",
            }
            return httpx.Response(200, json=_login_response())
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        permission_path = (
            f"/sources/{prepared.source_id}/versions/"
            f"{prepared.source_version_id}/permission-requests"
        )
        if request.url.path == permission_path:
            assert body == prepared.command.model_dump(mode="json")
            assert "content" not in body
            assert request.headers["idempotency-key"] == prepared.idempotency_key
            return httpx.Response(
                201,
                json=_permission_response(prepared),
            )
        assert request.url.path == "/auth/logout"
        assert body == {}
        return httpx.Response(200, json={"revoked": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: OPERATOR_PASSWORD)

    assert cli.main(_arguments("--execute", "--json")) == 0
    output = capsys.readouterr().out
    for secret in (OPERATOR_PASSWORD, TOKEN, SOURCE_CONTENT, cli._RIGHTS_BASIS):
        assert secret not in output
    report = json.loads(output)
    assert report["status"] == "succeeded"
    assert report["permission_requests_sent"] == 1
    assert report["requests"][0]["sequence"] == 2
    assert report["requests"][0]["request_checksum"] == "a" * 64
    assert [request.url.path for request in requests] == [
        "/auth/login",
        (
            f"/sources/{prepared.source_id}/versions/"
            f"{prepared.source_version_id}/permission-requests"
        ),
        "/auth/logout",
    ]


def test_safe_replay_requires_explicit_header_and_matching_view(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = cli._prepare_permission_request(_source_submission())
    _patch_preflight(monkeypatch, prepared)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json=_login_response())
        if request.url.path.endswith("/permission-requests"):
            return httpx.Response(
                200,
                headers={"Idempotency-Replayed": "true"},
                json=_permission_response(prepared),
            )
        return httpx.Response(200, json={"revoked": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: OPERATOR_PASSWORD)

    assert cli.main(_arguments("--execute", "--json")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["requests"][0]["status"] == "idempotent_replay"


@pytest.mark.parametrize(
    "overrides",
    [
        {"sequence": 1},
        {"requested_extraction_permission": "selected_sections"},
        {"rights_basis": "broader rights"},
        {"request_checksum": "not-a-sha256"},
        {"supersedes_request_id": None},
        {"actor_role_snapshot": ["reviewer"]},
    ],
)
def test_response_contract_mismatch_fails_and_logout_still_runs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, object],
) -> None:
    prepared = cli._prepare_permission_request(_source_submission())
    _patch_preflight(monkeypatch, prepared)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/auth/login":
            return httpx.Response(200, json=_login_response())
        if request.url.path.endswith("/permission-requests"):
            return httpx.Response(
                201,
                json=_permission_response(prepared, **overrides),
            )
        return httpx.Response(200, json={"revoked": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: OPERATOR_PASSWORD)

    assert cli.main(_arguments("--execute", "--json")) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["requests"][0]["error"]["code"] in {
        "invalid_permission_request_response",
        "permission_request_response_mismatch",
    }
    assert paths[-1] == "/auth/logout"


def test_wrong_role_response_is_rejected_and_issued_token_is_revoked(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = cli._prepare_permission_request(_source_submission())
    _patch_preflight(monkeypatch, prepared)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/auth/login":
            return httpx.Response(200, json=_login_response(roles=["reviewer"]))
        assert request.url.path == "/auth/logout"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json={"revoked": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: OPERATOR_PASSWORD)

    assert cli.main(_arguments("--execute", "--json")) == 1
    output = capsys.readouterr().out
    assert TOKEN not in output
    report = json.loads(output)
    assert report["login"]["error"]["code"] == (
        "operator_login_response_invalid"
    )
    assert report["permission_requests_sent"] == 0
    assert paths == ["/auth/login", "/auth/logout"]


def test_logout_revoked_false_makes_successful_request_a_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = cli._prepare_permission_request(_source_submission())
    _patch_preflight(monkeypatch, prepared)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json=_login_response())
        if request.url.path.endswith("/permission-requests"):
            return httpx.Response(201, json=_permission_response(prepared))
        return httpx.Response(200, json={"revoked": False})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: OPERATOR_PASSWORD)

    assert cli.main(_arguments("--execute", "--json")) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "partial_failure"
    assert report["logout"] == {
        "status": "failed",
        "error": {"code": "logout_not_revoked"},
    }


def test_server_body_token_password_and_source_content_are_never_rendered(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared = cli._prepare_permission_request(_source_submission())
    _patch_preflight(monkeypatch, prepared)
    server_secret = (
        f"never expose {TOKEN} {OPERATOR_PASSWORD} {SOURCE_CONTENT} "
        f"{cli._RIGHTS_BASIS}"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json=_login_response())
        if request.url.path.endswith("/permission-requests"):
            return httpx.Response(
                403,
                json={
                    "detail": {
                        "code": "permission_denied",
                        "message": server_secret,
                    }
                },
            )
        return httpx.Response(200, json={"revoked": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: OPERATOR_PASSWORD)

    assert cli.main(_arguments("--execute", "--json")) == 1
    output = capsys.readouterr().out
    for secret in (
        TOKEN,
        OPERATOR_PASSWORD,
        SOURCE_CONTENT,
        cli._RIGHTS_BASIS,
        server_secret,
    ):
        assert secret not in output
    report = json.loads(output)
    assert report["requests"][0]["error"] == {
        "code": "http_error",
        "status_code": 403,
        "api_code": "permission_denied",
    }
