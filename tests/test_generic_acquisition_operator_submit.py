"""Security-boundary tests for generic ledger Source submission."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest

import research.acquisition.operator_submit as cli
from research.acquisition.generic_staging import stage_generic_acquisition


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def _staged(tmp_path: Path) -> tuple[Path, str]:
    discovery = tmp_path / "discovery.json"
    content = tmp_path / "content.json"
    output = tmp_path / "staging"
    url = "https://example.test/papers/attention"
    _write_json(
        discovery,
        {
            "results": [
                {
                    "title": "Attention in a controlled magic performance",
                    "url": url,
                    "authors": ["A. Researcher"],
                    "published_year": 2025,
                    "venue": "Journal of Test Cognition",
                    "doi": "10.1000/magicforge.generic",
                    "snippet": "Discovery text only.",
                    "provider": "exa",
                    "tool_name": "web_search_exa",
                    "query": "attention magic performance",
                    "retrieved_at": "2026-08-09T01:00:00+00:00",
                    "rank": 1,
                    "source_category": "academic",
                    "source_type": "journal_article",
                    "knowledge_origin": "scientific_evidence",
                    "peer_review_status": "peer_reviewed",
                    "sensitive_information_level": "controlled",
                }
            ]
        },
    )
    _write_json(
        content,
        {
            "sources": [
                {
                    "requested_url": url,
                    "returned_url": url,
                    "title": "Attention in a controlled magic performance",
                    "content": (
                        "Introduction\nMethod\nParticipants completed the task.\n"
                        "Results\nAttention predicted detection.\nDiscussion\n"
                        "The controlled context limits generalization."
                    ),
                    "content_access": "full_text",
                    "provider": "exa",
                    "retrieved_at": "2026-08-09T01:10:00+00:00",
                    "redirect_verified": True,
                    "access": {
                        "access_method": "publisher full text",
                        "redistribution_allowed": False,
                        "permission_notes": "Internal Source review only.",
                    },
                }
            ]
        },
    )
    report = stage_generic_acquisition([discovery], content, output)
    return output, report["ledger_sha256"]


def _arguments(staging: Path, ledger: str, *extra: str) -> list[str]:
    return [
        "--staging-dir",
        str(staging),
        "--ledger-sha256",
        ledger,
        *extra,
    ]


def _source_response(prepared: cli.PreparedGenericSource) -> dict[str, object]:
    version_id = uuid5(
        NAMESPACE_URL,
        (
            f"magicforge:source-version:{prepared.expected_source_id}:"
            f"{prepared.content_sha256}"
        ),
    )
    return {
        "id": str(prepared.expected_source_id),
        "canonical_key": prepared.canonical_key,
        "title": prepared.command.citation.title,
        "source_category": prepared.command.source_category.value,
        "versions": [
            {
                "id": str(version_id),
                "version": 1,
                "content_hash": prepared.content_sha256,
                "content_access": prepared.command.content_access.value,
                "source_type": prepared.command.source_type.value,
                "knowledge_origin": prepared.command.knowledge_origin.value,
                "sensitivity": "controlled",
                "citation_status": "unverified",
                "status": "submitted",
                "latest_permission_request_id": None,
                "created_at": datetime.now(UTC).isoformat(),
            }
        ],
    }


@pytest.mark.parametrize(
    "value",
    ["https://api.example.test", "http://api.example.test"],
)
def test_submission_api_must_be_loopback(value) -> None:
    with pytest.raises(cli.CliInputError):
        cli._normalize_local_api_url(value)


def test_dry_run_verifies_ledger_without_credentials_or_http(
    tmp_path, monkeypatch, capsys
) -> None:
    staging, ledger = _staged(tmp_path)

    monkeypatch.setattr(
        cli,
        "_new_client",
        lambda _url: (_ for _ in ()).throw(AssertionError("HTTP forbidden")),
    )
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("prompt forbidden")),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("prompt forbidden")),
    )

    result = cli.main(_arguments(staging, ledger, "--json"))

    assert result == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "dry_run"
    assert report["batch"]["ledger_sha256"] == ledger
    assert report["preflight"]["payload_count"] == 1
    assert report["source_requests_sent"] == 0
    rendered = json.dumps(report)
    assert "Participants completed" not in rendered
    assert "Discovery text" not in rendered


def test_execute_requires_exact_second_ledger_confirmation_before_credentials(
    tmp_path, monkeypatch, capsys
) -> None:
    staging, ledger = _staged(tmp_path)
    monkeypatch.setattr(
        cli,
        "_new_client",
        lambda _url: (_ for _ in ()).throw(AssertionError("HTTP forbidden")),
    )
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("prompt forbidden")),
    )

    result = cli.main(
        _arguments(
            staging,
            ledger,
            "--execute",
            "--confirm-ledger-sha256",
            "0" * 64,
            "--json",
        )
    )

    assert result == 2
    report = json.loads(capsys.readouterr().out)
    assert report["preflight"]["error"]["code"] == "invalid_input"
    assert report["source_requests_sent"] == 0


def test_payload_checksum_mismatch_fails_closed(tmp_path) -> None:
    staging, ledger = _staged(tmp_path)
    ledger_document = json.loads(
        (staging / "ledgers" / f"{ledger}.json").read_text(encoding="utf-8")
    )
    relative = ledger_document["entries"][0]["source_submission"]["payload_file"]
    (staging / relative).write_text("{}", encoding="utf-8")

    with pytest.raises(cli.GenericSubmissionPreflightError) as failure:
        cli.preflight_generic_ledger(staging, ledger)

    assert failure.value.code == "source_payload_hash_mismatch"


def test_older_valid_ledger_is_rejected_after_a_new_staging_revision(tmp_path) -> None:
    staging, old_ledger = _staged(tmp_path)
    content_path = tmp_path / "content.json"
    content = json.loads(content_path.read_text(encoding="utf-8"))
    content["sources"][0]["access"]["permission_notes"] = (
        "Revised internal Source review note."
    )
    _write_json(content_path, content)
    revised = stage_generic_acquisition(
        [tmp_path / "discovery.json"], content_path, staging
    )

    with pytest.raises(cli.GenericSubmissionPreflightError) as failure:
        cli.preflight_generic_ledger(staging, old_ledger)

    assert failure.value.code == "ledger_superseded"
    prepared, batch = cli.preflight_generic_ledger(
        staging, revised["ledger_sha256"]
    )
    assert len(prepared) == 1
    assert batch.ledger_sha256 == revised["ledger_sha256"]


def test_execute_uses_cookie_csrf_submits_only_sources_and_logs_out(
    tmp_path, monkeypatch, capsys
) -> None:
    staging, ledger = _staged(tmp_path)
    prepared, _batch = cli.preflight_generic_ledger(staging, ledger)
    source = prepared[0]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        assert request.headers["origin"] == "http://127.0.0.1:8000"
        assert "authorization" not in request.headers
        if request.url.path == "/auth/login":
            assert body == {
                "identifier": "CorpusOperator",
                "password": "operator private password",
                "transport": "cookie",
                "device_label": "generic-source-submitter",
            }
            return httpx.Response(
                200,
                headers=[
                    (
                        "Set-Cookie",
                        "magicforge_session=session-private; Path=/; HttpOnly; SameSite=Lax",
                    ),
                    (
                        "Set-Cookie",
                        "magicforge_csrf=csrf-private; Path=/; SameSite=Lax",
                    ),
                ],
                json={
                    "actor": {"roles": ["operator"]},
                    "transport": "cookie",
                    "access_token": None,
                    "csrf_required": True,
                },
            )
        assert "magicforge_session=session-private" in request.headers["cookie"]
        assert "magicforge_csrf=csrf-private" in request.headers["cookie"]
        assert request.headers["x-csrf-token"] == "csrf-private"
        if request.url.path == "/sources":
            assert request.headers["idempotency-key"] == source.idempotency_key
            assert body == source.payload
            return httpx.Response(201, json=_source_response(source))
        assert request.url.path == "/auth/logout"
        assert body == {}
        return httpx.Response(200, json={"revoked": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setenv("MAGICFORGE_OPERATOR_IDENTIFIER", "CorpusOperator")
    monkeypatch.setenv("MAGICFORGE_OPERATOR_PASSWORD", "operator private password")
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("prompt forbidden")),
    )

    result = cli.main(
        _arguments(
            staging,
            ledger,
            "--execute",
            "--confirm-ledger-sha256",
            ledger,
            "--json",
        )
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "operator private password" not in output
    assert "session-private" not in output
    assert "csrf-private" not in output
    report = json.loads(output)
    assert report["status"] == "succeeded"
    assert report["login"] == {
        "roles": ["operator"],
        "status": "succeeded",
        "transport": "cookie",
    }
    assert report["submissions"][0]["workflow_status"] == "submitted"
    assert report["logout"] == {"revoked": True, "status": "succeeded"}
    assert [request.url.path for request in requests] == [
        "/auth/login",
        "/sources",
        "/auth/logout",
    ]


def test_non_operator_cookie_session_never_submits_source(
    tmp_path, monkeypatch, capsys
) -> None:
    staging, ledger = _staged(tmp_path)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/auth/login":
            return httpx.Response(
                200,
                headers=[
                    ("Set-Cookie", "magicforge_session=session; Path=/; HttpOnly"),
                    ("Set-Cookie", "magicforge_csrf=csrf; Path=/"),
                ],
                json={
                    "actor": {"roles": ["reviewer"]},
                    "transport": "cookie",
                    "access_token": None,
                    "csrf_required": True,
                },
            )
        assert request.url.path == "/auth/logout"
        return httpx.Response(200, json={"revoked": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(cli, "_new_client", lambda _url: client)
    monkeypatch.setenv("MAGICFORGE_OPERATOR_IDENTIFIER", "WrongRole")
    monkeypatch.setenv("MAGICFORGE_OPERATOR_PASSWORD", "private password")

    result = cli.main(
        _arguments(
            staging,
            ledger,
            "--execute",
            "--confirm-ledger-sha256",
            ledger,
            "--json",
        )
    )

    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["login"]["error"]["code"] == (
        "operator_cookie_login_response_invalid"
    )
    assert report["source_requests_sent"] == 0
    assert report["logout"] == {"revoked": True, "status": "succeeded"}
    assert paths == ["/auth/login", "/auth/logout"]
