"""Security-boundary tests for governance account provisioning CLI."""

from __future__ import annotations

import json
from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest

import security.governance_account_cli as cli


def _arguments(*extra: str) -> list[str]:
    return [
        "provision-users",
        "--admin-identifier",
        "ArchiveAdmin",
        "--reviewer-username",
        "ReviewCustodian",
        "--reviewer-email",
        "reviewer@example.test",
        "--operator-username",
        "ReleaseOperator",
        "--operator-email",
        "operator@example.test",
        *extra,
    ]


def _password_reader(values: list[str]):
    iterator: Iterator[str] = iter(values)
    return lambda _prompt: next(iterator)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("http://localhost:8000/api/", "http://localhost:8000/api"),
        ("https://api.example.test", "https://api.example.test"),
    ],
)
def test_api_url_allows_loopback_http_or_any_https(value, expected) -> None:
    assert cli._normalize_api_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://api.example.test",
        "ftp://127.0.0.1",
        "https://user:password@example.test",
        "https://example.test?token=secret",
        "https://example.test/#fragment",
    ],
)
def test_api_url_rejects_unsafe_transport_or_embedded_data(value) -> None:
    with pytest.raises(cli.CliInputError):
        cli._normalize_api_url(value)


def test_http_client_does_not_inherit_environment_proxy_settings(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def client_constructor(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(cli.httpx, "Client", client_constructor)

    assert cli._new_client("http://127.0.0.1:8000") is sentinel
    assert captured["trust_env"] is False


def test_dry_run_does_not_prompt_or_send_requests(monkeypatch, capsys) -> None:
    def forbidden_prompt(_prompt: str) -> str:
        raise AssertionError("dry-run must not prompt for credentials")

    def forbidden_client(_api_url: str):
        raise AssertionError("dry-run must not create an HTTP client")

    monkeypatch.setattr(cli.getpass, "getpass", forbidden_prompt)
    monkeypatch.setattr(cli, "_new_client", forbidden_client)

    exit_code = cli.main(_arguments("--dry-run", "--json"))

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "dry_run"
    assert report["requests_sent"] == 0
    assert report["accounts"][0]["roles"] == ["reviewer"]
    assert report["accounts"][1]["roles"] == ["operator"]
    assert "password" not in json.dumps(report).casefold()


def test_success_sends_exact_separate_role_payloads_and_logs_out(
    monkeypatch, capsys
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if request.url.path == "/auth/login":
            assert body == {
                "identifier": "ArchiveAdmin",
                "password": "admin hidden password",
                "transport": "bearer",
                "device_label": "governance-account-cli",
            }
            assert "authorization" not in request.headers
            return httpx.Response(
                200,
                json={
                    "transport": "bearer",
                    "access_token": "in-memory-bearer-token",
                },
            )
        assert request.headers["authorization"] == (
            "Bearer in-memory-bearer-token"
        )
        if request.url.path == "/admin/users":
            role = body["roles"][0]
            assert body["roles"] in (["reviewer"], ["operator"])
            expected_password = (
                "reviewer hidden password"
                if role == "reviewer"
                else "operator hidden password"
            )
            assert body["password"] == expected_password
            return httpx.Response(
                201,
                json={
                    "id": str(uuid4()),
                    "username": body["username"],
                    "roles": [role],
                },
            )
        assert request.url.path == "/auth/logout"
        assert body == {}
        return httpx.Response(200, json={"revoked": True})

    client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(cli, "_new_client", lambda _api_url: client)
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        _password_reader(
            [
                "admin hidden password",
                "reviewer hidden password",
                "reviewer hidden password",
                "operator hidden password",
                "operator hidden password",
            ]
        ),
    )

    exit_code = cli.main(_arguments("--json"))

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "succeeded"
    assert report["reviewer"]["roles"] == ["reviewer"]
    assert report["operator"]["roles"] == ["operator"]
    assert report["logout"] == {"status": "succeeded", "revoked": True}
    assert [request.url.path for request in requests] == [
        "/auth/login",
        "/admin/users",
        "/admin/users",
        "/auth/logout",
    ]


def test_partial_failure_is_clear_and_response_secrets_are_redacted(
    monkeypatch, capsys
) -> None:
    token = "server-returned-secret-token"
    reviewer_password = "reviewer private password"
    operator_password = "operator private password"
    create_count = 0
    logout_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_count, logout_called
        body = json.loads(request.content)
        if request.url.path == "/auth/login":
            return httpx.Response(
                200,
                json={"transport": "bearer", "access_token": token},
            )
        if request.url.path == "/admin/users":
            create_count += 1
            if create_count == 1:
                return httpx.Response(
                    201,
                    json={
                        "id": str(uuid4()),
                        "username": body["username"],
                        "roles": ["reviewer"],
                    },
                )
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "code": "state_conflict",
                        "message": (
                            f"do not expose {token} {reviewer_password} "
                            f"{operator_password}"
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
    monkeypatch.setattr(cli, "_new_client", lambda _api_url: client)
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        _password_reader(
            [
                "admin private password",
                reviewer_password,
                reviewer_password,
                operator_password,
                operator_password,
            ]
        ),
    )

    exit_code = cli.main(_arguments("--json"))

    assert exit_code == 1
    output = capsys.readouterr().out
    assert token not in output
    assert reviewer_password not in output
    assert operator_password not in output
    assert "admin private password" not in output
    report = json.loads(output)
    assert report["status"] == "partial_failure"
    assert report["reviewer"]["status"] == "created"
    assert report["operator"] == {
        "status": "failed",
        "error": {
            "code": "http_error",
            "status_code": 409,
            "api_code": "state_conflict",
        },
    }
    assert report["logout"]["status"] == "succeeded"
    assert logout_called is True


def test_same_account_cannot_receive_both_governance_roles(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        cli,
        "_new_client",
        lambda _api_url: (_ for _ in ()).throw(
            AssertionError("invalid input must not send requests")
        ),
    )
    arguments = _arguments("--json")
    operator_index = arguments.index("--operator-username") + 1
    arguments[operator_index] = "reviewcustodian"

    exit_code = cli.main(arguments)

    assert exit_code == 2
    report = json.loads(capsys.readouterr().out)
    assert report["error"]["code"] == "invalid_input"
