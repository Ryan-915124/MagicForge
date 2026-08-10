"""Security-boundary tests for the governed Source submission CLI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest

import security.governance_source_cli as cli


OPERATOR_PASSWORD = "a hidden operator passphrase"
TOKEN = "server-secret-bearer-token"


def _wrapper(
    *,
    marker: str = "one",
    content: str | None = None,
) -> dict:
    doi = f"10.1000/magicforge.{marker}"
    candidate_id = uuid5(NAMESPACE_URL, f"magicforge:research:{doi}")
    source_content = content or f"Exact governed Source content for {marker}."
    digest = hashlib.sha256(source_content.encode("utf-8")).hexdigest()
    return {
        "candidate_id": str(candidate_id),
        "expected_production_source_id": str(candidate_id),
        "content_sha256": digest,
        "idempotency_key": f"corpus-run-001:source:{candidate_id}:{digest}",
        "payload": {
            "schema_version": "source-workflow-0.1",
            "citation": {
                "title": f"Controlled source {marker}",
                "authors": ["A. Researcher"],
                "year": 2024,
                "venue": "Journal of Test Cognition",
                "volume": "1",
                "issue": "2",
                "pages": "3-4",
                "doi": doi,
                "url": f"https://example.test/{marker}",
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
            "content": source_content,
            "content_access": "web_extract",
            "access": {
                "access_method": "publisher full text",
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
        },
    }


def _write_wrapper(directory: Path, wrapper: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{wrapper['candidate_id']}.json"
    path.write_text(json.dumps(wrapper), encoding="utf-8")
    return path


def _canonical_hash(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _write_batch_documents(payload_dir: Path) -> Path:
    run_dir = payload_dir.parent
    wrappers = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(payload_dir.glob("*.json"))
    ]
    manifest = {
        "schema_version": "source-acquisition-manifest-0.1",
        "run_id": "production-source-acquisition-001",
        "source_run_id": "magicforge-corpus-run-001",
    }
    manifest["manifest_sha256"] = _canonical_hash(manifest)
    (run_dir / "acquisition-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    items = [
        {
            "candidate_id": wrapper["candidate_id"],
            "content_sha256": wrapper["content_sha256"],
            "payload_sha256": hashlib.sha256(
                (payload_dir / f"{wrapper['candidate_id']}.json").read_bytes()
            ).hexdigest(),
            "payload_status": "prepared",
            "payload_file": (
                f"source_payloads/{wrapper['candidate_id']}.json"
            ),
            "payload_blocker": None,
            "validation": {"passed": True},
        }
        for wrapper in wrappers
    ]
    results = {
        "schema_version": "source-acquisition-results-0.2",
        "run_id": "production-source-acquisition-001",
        "source_manifest": "acquisition-manifest.json",
        "source_manifest_sha256": manifest["manifest_sha256"],
        "constraints": {
            "saved_exports_only": True,
            "network_access": False,
            "api_submission": False,
            "database_write": False,
            "review_decision_created": False,
            "unresolved_pdf_export_processed": False,
            "requested_extraction_permission": "none",
            "requested_storage_permission": "none",
            "sensitive_information_level": "controlled",
            "citation_verification": [],
            "redistribution_allowed": False,
        },
        "summary": {
            "selected_results": len(items),
            "source_payloads": len(items),
        },
        "items": items,
    }
    results["results_sha256"] = _canonical_hash(results)
    results_path = run_dir / "acquisition-results.json"
    results_path.write_text(json.dumps(results), encoding="utf-8")
    return results_path


def _patch_batch_loader(
    monkeypatch,
    payload_dir: Path,
    *,
    wrappers: list[dict] | None = None,
) -> None:
    """Replace only the official-bundle gate in narrow unit tests.

    The real-bundle tests below deliberately do not use this helper.  Other
    tests exercise payload and HTTP behavior with tiny fixtures, while still
    preserving the wrapper-byte commitment enforced by preflight.
    """

    wrapper_by_name = {
        f"{wrapper['candidate_id']}.json": wrapper
        for wrapper in (wrappers or [])
    }
    commitments: dict[str, cli.OfficialPayloadCommitment] = {}
    for path in sorted(payload_dir.glob("*.json")):
        raw = path.read_bytes()
        wrapper = wrapper_by_name.get(path.name)
        if wrapper is None:
            wrapper = json.loads(raw)
        commitments[path.name] = cli.OfficialPayloadCommitment(
            candidate_id=cli.UUID(wrapper["candidate_id"]),
            content_sha256=wrapper["content_sha256"],
            payload_sha256=hashlib.sha256(raw).hexdigest(),
        )

    def load_batch(results_file: Path, resolved_payload_dir: Path):
        assert resolved_payload_dir == payload_dir.resolve()
        return cli.OfficialAcquisitionBatch(
            results_file=results_file,
            results_sha256=cli._OFFICIAL_RESULTS_SHA256,
            source_manifest_sha256=cli._OFFICIAL_MANIFEST_SHA256,
            expected_by_file=commitments,
        )

    monkeypatch.setattr(cli, "_load_official_batch", load_batch)


def _rewrite_results(results_path: Path, mutate, *, rehash: bool = True) -> None:
    results = json.loads(results_path.read_text(encoding="utf-8"))
    mutate(results)
    if rehash:
        results.pop("results_sha256", None)
        results["results_sha256"] = _canonical_hash(results)
    results_path.write_text(json.dumps(results), encoding="utf-8")


def _forbid_network_and_prompt(monkeypatch) -> None:
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


def _arguments(payload_dir: Path, *extra: str) -> list[str]:
    results_file = payload_dir.parent / "acquisition-results.json"
    if not results_file.exists():
        _write_batch_documents(payload_dir)
    return [
        "submit",
        "--operator-identifier",
        "ReleaseOperator",
        "--payload-dir",
        str(payload_dir),
        "--results-file",
        str(results_file),
        *extra,
    ]


def _response(wrapper: dict) -> dict:
    source_id = wrapper["expected_production_source_id"]
    version_id = uuid5(
        NAMESPACE_URL,
        f"magicforge:source-version:{source_id}:{wrapper['content_sha256']}",
    )
    payload = wrapper["payload"]
    return {
        "id": source_id,
        "canonical_key": f"doi:{payload['citation']['doi']}",
        "title": payload["citation"]["title"],
        "source_category": payload["source_category"],
        "versions": [
            {
                "id": str(version_id),
                "version": 1,
                "content_hash": wrapper["content_sha256"],
                "content_access": payload["content_access"],
                "source_type": payload["source_type"],
                "knowledge_origin": payload["knowledge_origin"],
                "sensitivity": "controlled",
                "citation_status": "unverified",
                "status": "submitted",
                "created_at": "2026-08-08T00:00:00Z",
            }
        ],
    }


def _login_response(*, roles: list[str] | None = None) -> dict:
    return {
        "transport": "bearer",
        "access_token": TOKEN,
        "actor": {"roles": roles or ["operator"]},
    }


def test_http_client_does_not_inherit_proxy_settings(monkeypatch) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def client_constructor(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(cli.httpx, "Client", client_constructor)

    assert cli._new_client("http://127.0.0.1:8000") is sentinel
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False


def test_default_is_offline_dry_run_without_password_prompt(
    tmp_path, monkeypatch, capsys
) -> None:
    payload_dir = tmp_path / "source_payloads"
    wrapper = _wrapper(content="private exact source body")
    _write_wrapper(payload_dir, wrapper)
    _patch_batch_loader(monkeypatch, payload_dir)

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
            AssertionError("dry-run must not create an HTTP client")
        ),
    )

    exit_code = cli.main(_arguments(payload_dir, "--json"))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "private exact source body" not in output
    report = json.loads(output)
    assert report["status"] == "dry_run"
    assert report["source_requests_sent"] == 0
    assert report["preflight"] == {"status": "passed", "payload_count": 1}


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda value: value["payload"].update({"approved": True}),
            "forbidden_governance_control_field",
        ),
        (
            lambda value: value["payload"].update(
                {"requested_extraction_permission": "full_text"}
            ),
            "extraction_permission_not_none",
        ),
        (
            lambda value: value["payload"].update(
                {"requested_storage_permission": "derived_knowledge_only"}
            ),
            "storage_permission_not_none",
        ),
        (
            lambda value: value["payload"].update(
                {
                    "citation_verification": [
                        {
                            "scope": "metadata",
                            "method": "doi_resolver",
                            "resolver_result": "matched",
                            "verified_identifier": "10.1000/test",
                            "checked_locator": "https://doi.org/10.1000/test",
                            "resolver_name": "Crossref",
                        }
                    ]
                }
            ),
            "citation_verification_not_empty",
        ),
        (
            lambda value: value["payload"].update(
                {"sensitive_information_level": "public"}
            ),
            "sensitivity_not_controlled",
        ),
        (
            lambda value: value["payload"].update({"content_access": "abstract"}),
            "content_access_not_web_extract",
        ),
        (
            lambda value: value["payload"]["access"].update(
                {"redistribution_allowed": True}
            ),
            "redistribution_must_be_false",
        ),
    ],
)
def test_unsafe_payload_blocks_entire_batch_before_login(
    tmp_path, monkeypatch, capsys, mutate, expected_code
) -> None:
    payload_dir = tmp_path / "source_payloads"
    _write_wrapper(payload_dir, _wrapper(marker="valid"))
    invalid = _wrapper(marker="invalid")
    mutate(invalid)
    _write_wrapper(payload_dir, invalid)
    _patch_batch_loader(monkeypatch, payload_dir)

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

    exit_code = cli.main(_arguments(payload_dir, "--execute", "--json"))

    assert exit_code == 2
    report = json.loads(capsys.readouterr().out)
    assert report["preflight"]["status"] == "failed"
    assert expected_code in {item["code"] for item in report["preflight"]["errors"]}
    assert report["source_requests_sent"] == 0


def test_duplicate_json_key_is_rejected_without_leaking_content(
    tmp_path, monkeypatch, capsys
) -> None:
    payload_dir = tmp_path / "source_payloads"
    wrapper = _wrapper(content="never render this exact content")
    path = _write_wrapper(payload_dir, wrapper)
    _write_batch_documents(payload_dir)
    serialized = json.dumps(wrapper)
    path.write_text(
        serialized[:-1] + ', "candidate_id": "duplicate"}',
        encoding="utf-8",
    )
    _patch_batch_loader(monkeypatch, payload_dir, wrappers=[wrapper])

    exit_code = cli.main(_arguments(payload_dir, "--json"))

    assert exit_code == 2
    output = capsys.readouterr().out
    assert "never render this exact content" not in output
    report = json.loads(output)
    assert report["preflight"]["errors"][0]["code"] == "duplicate_json_key"


def test_self_rehashed_fake_bundle_is_rejected_by_official_anchor(
    tmp_path, monkeypatch, capsys
) -> None:
    """A self-consistent attacker-controlled bundle is not an authority."""

    payload_dir = tmp_path / "source_payloads"
    _write_wrapper(payload_dir, _wrapper())
    results_path = _write_batch_documents(payload_dir)
    _rewrite_results(
        results_path,
        lambda value: value["summary"].update({"source_payloads": 999}),
    )
    _forbid_network_and_prompt(monkeypatch)

    assert cli.main(_arguments(payload_dir, "--execute", "--json")) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["preflight"]["errors"] == [
        {
            "file": "acquisition-results.json",
            "code": "acquisition_results_trust_anchor_mismatch",
        }
    ]
    assert report["source_requests_sent"] == 0


def test_actual_wrapper_bytes_must_match_staged_payload_commitment(
    tmp_path,
) -> None:
    payload_dir = tmp_path / "source_payloads"
    path = _write_wrapper(payload_dir, _wrapper())

    with pytest.raises(cli.PayloadPreflightError) as caught:
        cli._prepare_payload(path, expected_payload_sha256="f" * 64)

    assert caught.value.code == "payload_file_hash_mismatch"


@pytest.mark.parametrize("change", ["missing", "extra"])
def test_payload_directory_must_equal_complete_prepared_result_set(
    tmp_path, monkeypatch, capsys, change
) -> None:
    payload_dir = tmp_path / "source_payloads"
    first_path = _write_wrapper(payload_dir, _wrapper(marker="first"))
    _patch_batch_loader(monkeypatch, payload_dir)
    if change == "missing":
        first_path.unlink()
        expected_code = "prepared_payload_missing"
    else:
        _write_wrapper(payload_dir, _wrapper(marker="unlisted"))
        expected_code = "unexpected_payload_file"
    _forbid_network_and_prompt(monkeypatch)

    assert cli.main(_arguments(payload_dir, "--execute", "--json")) == 2
    report = json.loads(capsys.readouterr().out)
    assert expected_code in {
        item["code"] for item in report["preflight"]["errors"]
    }
    assert report["source_requests_sent"] == 0


def test_official_v02_bundle_dry_run_is_exactly_29_and_offline(
    monkeypatch, capsys, synthetic_official_cli_bundle
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
        [
            "submit",
            "--operator-identifier",
            "DryRunOperator",
            "--json",
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "dry_run"
    assert report["preflight"] == {"status": "passed", "payload_count": 29}
    assert report["source_requests_sent"] == 0
    assert report["batch"] == {
        "run_id": "production-source-acquisition-001",
        "results_sha256": cli._OFFICIAL_RESULTS_SHA256,
        "source_manifest_sha256": cli._OFFICIAL_MANIFEST_SHA256,
        "payload_count": 29,
    }


def test_execute_posts_only_sources_with_idempotency_then_logs_out(
    tmp_path, monkeypatch, capsys
) -> None:
    payload_dir = tmp_path / "source_payloads"
    wrapper = _wrapper(content="exact source body must stay out of output")
    _write_wrapper(payload_dir, wrapper)
    _patch_batch_loader(monkeypatch, payload_dir)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if request.url.path == "/auth/login":
            assert body == {
                "identifier": "ReleaseOperator",
                "password": OPERATOR_PASSWORD,
                "transport": "bearer",
                "device_label": "governance-source-cli",
            }
            return httpx.Response(200, json=_login_response())
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if request.url.path == "/sources":
            assert body == wrapper["payload"]
            assert request.headers["idempotency-key"] == wrapper["idempotency_key"]
            return httpx.Response(201, json=_response(wrapper))
        assert request.url.path == "/auth/logout"
        assert body == {}
        return httpx.Response(200, json={"revoked": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: OPERATOR_PASSWORD)

    exit_code = cli.main(_arguments(payload_dir, "--execute", "--json"))

    assert exit_code == 0
    output = capsys.readouterr().out
    assert OPERATOR_PASSWORD not in output
    assert TOKEN not in output
    assert wrapper["payload"]["content"] not in output
    report = json.loads(output)
    assert report["status"] == "succeeded"
    assert report["submissions"][0]["status"] == (
        "accepted_or_idempotent_replay"
    )
    assert report["submissions"][0]["workflow_status"] == "submitted"
    assert report["submissions"][0]["content_sha256"] == wrapper["content_sha256"]
    assert [request.url.path for request in requests] == [
        "/auth/login",
        "/sources",
        "/auth/logout",
    ]


def test_safe_200_replay_requires_explicit_header_and_matching_response(
    tmp_path, monkeypatch, capsys
) -> None:
    payload_dir = tmp_path / "source_payloads"
    wrapper = _wrapper()
    _write_wrapper(payload_dir, wrapper)
    _patch_batch_loader(monkeypatch, payload_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json=_login_response())
        if request.url.path == "/sources":
            return httpx.Response(
                200,
                headers={"Idempotency-Replayed": "true"},
                json=_response(wrapper),
            )
        return httpx.Response(200, json={"revoked": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: OPERATOR_PASSWORD)

    assert cli.main(_arguments(payload_dir, "--execute", "--json")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["submissions"][0]["status"] == "idempotent_replay"


def test_unmarked_200_source_response_is_a_failure_but_logout_still_runs(
    tmp_path, monkeypatch, capsys
) -> None:
    payload_dir = tmp_path / "source_payloads"
    wrapper = _wrapper()
    _write_wrapper(payload_dir, wrapper)
    _patch_batch_loader(monkeypatch, payload_dir)
    logout_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal logout_called
        if request.url.path == "/auth/login":
            return httpx.Response(200, json=_login_response())
        if request.url.path == "/sources":
            return httpx.Response(200, json=_response(wrapper))
        logout_called = True
        return httpx.Response(200, json={"revoked": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: OPERATOR_PASSWORD)

    assert cli.main(_arguments(payload_dir, "--execute", "--json")) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["submissions"][0]["error"]["status_code"] == 200
    assert logout_called is True


def test_logout_revoked_false_is_a_failure(
    tmp_path, monkeypatch, capsys
) -> None:
    payload_dir = tmp_path / "source_payloads"
    wrapper = _wrapper()
    _write_wrapper(payload_dir, wrapper)
    _patch_batch_loader(monkeypatch, payload_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json=_login_response())
        if request.url.path == "/sources":
            return httpx.Response(201, json=_response(wrapper))
        assert request.url.path == "/auth/logout"
        return httpx.Response(200, json={"revoked": False})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: OPERATOR_PASSWORD)

    assert cli.main(_arguments(payload_dir, "--execute", "--json")) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "partial_failure"
    assert report["logout"] == {
        "status": "failed",
        "error": {"code": "logout_not_revoked"},
    }


def test_wrong_role_login_is_rejected_and_issued_token_is_revoked(
    tmp_path, monkeypatch, capsys
) -> None:
    payload_dir = tmp_path / "source_payloads"
    _write_wrapper(payload_dir, _wrapper())
    _patch_batch_loader(monkeypatch, payload_dir)
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

    assert cli.main(_arguments(payload_dir, "--execute", "--json")) == 1
    output = capsys.readouterr().out
    assert TOKEN not in output
    report = json.loads(output)
    assert report["login"]["error"]["code"] == "operator_login_response_invalid"
    assert paths == ["/auth/login", "/auth/logout"]


def test_partial_failure_continues_and_redacts_server_messages(
    tmp_path, monkeypatch, capsys
) -> None:
    payload_dir = tmp_path / "source_payloads"
    first = _wrapper(marker="a", content="first private exact text")
    second = _wrapper(marker="b", content="second private exact text")
    _write_wrapper(payload_dir, first)
    _write_wrapper(payload_dir, second)
    _patch_batch_loader(monkeypatch, payload_dir)
    source_count = 0
    logout_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal source_count, logout_called
        if request.url.path == "/auth/login":
            return httpx.Response(200, json=_login_response())
        if request.url.path == "/sources":
            body = json.loads(request.content)
            source_count += 1
            if source_count == 1:
                accepted = next(
                    item
                    for item in (first, second)
                    if item["payload"]["citation"]["doi"]
                    == body["citation"]["doi"]
                )
                return httpx.Response(201, json=_response(accepted))
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "code": "source_already_registered",
                        "message": (
                            f"never expose {TOKEN} {OPERATOR_PASSWORD} "
                            "second private exact text"
                        ),
                    }
                },
            )
        logout_called = True
        return httpx.Response(200, json={"revoked": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: OPERATOR_PASSWORD)

    assert cli.main(_arguments(payload_dir, "--execute", "--json")) == 1
    output = capsys.readouterr().out
    for secret in (
        TOKEN,
        OPERATOR_PASSWORD,
        "first private exact text",
        "second private exact text",
    ):
        assert secret not in output
    report = json.loads(output)
    assert report["status"] == "partial_failure"
    assert [item["status"] for item in report["submissions"]] == [
        "accepted_or_idempotent_replay",
        "failed",
    ]
    assert report["submissions"][1]["error"] == {
        "code": "http_error",
        "status_code": 409,
        "api_code": "source_already_registered",
    }
    assert source_count == 2
    assert logout_called is True


@pytest.mark.parametrize(
    "url",
    [
        "http://production.example.test",
        "https://user:secret@example.test",
        "https://example.test?token=secret",
    ],
)
def test_unsafe_api_urls_are_rejected_before_preflight_or_network(
    tmp_path, url, capsys
) -> None:
    payload_dir = tmp_path / "source_payloads"
    _write_wrapper(payload_dir, _wrapper())

    exit_code = cli.main(
        _arguments(payload_dir, "--api-url", url, "--execute", "--json")
    )

    assert exit_code == 2
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["code"] == "invalid_input"
    assert report["source_requests_sent"] == 0
