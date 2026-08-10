"""Safely provision distinct governance users through the administrator API.

Passwords are accepted only through hidden terminal prompts.  The administrator
Bearer token remains in process memory for the duration of the command and is
never included in command output, environment variables, or command arguments.
"""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


_SAFE_API_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_DEFAULT_API_URL = "http://127.0.0.1:8000"
_REQUEST_TIMEOUT_SECONDS = 15.0


class CliInputError(ValueError):
    """A safe, user-correctable command input error."""


class SafeApiError(RuntimeError):
    """An API failure containing only bounded, non-secret diagnostics."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        api_code: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.api_code = api_code

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code}
        if self.status_code is not None:
            result["status_code"] = self.status_code
        if self.api_code is not None:
            result["api_code"] = self.api_code
        return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m security.governance_account_cli",
        description=(
            "Provision separate MagicForge reviewer and operator accounts "
            "through the authenticated administrator API."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    provision = commands.add_parser(
        "provision-users",
        help="Create one reviewer and one operator without combining roles.",
    )
    provision.add_argument("--api-url", default=_DEFAULT_API_URL)
    provision.add_argument("--admin-identifier", required=True)
    provision.add_argument("--reviewer-username", required=True)
    provision.add_argument("--reviewer-email")
    provision.add_argument("--operator-username", required=True)
    provision.add_argument("--operator-email")
    provision.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Validate and print a redacted plan without prompting or sending "
            "requests."
        ),
    )
    provision.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Write one machine-readable, secret-free result object.",
    )
    return parser


def _is_loopback(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _normalize_api_url(value: str) -> str:
    raw_value = value.strip()
    try:
        parsed = urlsplit(raw_value)
        port = parsed.port
    except ValueError as exc:
        raise CliInputError("API URL is invalid.") from exc

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CliInputError("API URL must use HTTP or HTTPS and include a host.")
    if parsed.username is not None or parsed.password is not None:
        raise CliInputError("API URL must not contain credentials.")
    if parsed.query or parsed.fragment:
        raise CliInputError("API URL must not contain a query string or fragment.")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise CliInputError("Non-loopback API URLs must use HTTPS.")

    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def _required_text(value: str, label: str) -> str:
    result = value.strip()
    if not result:
        raise CliInputError(f"{label} must not be empty.")
    return result


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    result = value.strip()
    return result or None


def _validate_distinct_accounts(
    reviewer_username: str,
    reviewer_email: str | None,
    operator_username: str,
    operator_email: str | None,
) -> None:
    if reviewer_username.casefold() == operator_username.casefold():
        raise CliInputError(
            "Reviewer and operator usernames must identify different accounts."
        )
    if (
        reviewer_email is not None
        and operator_email is not None
        and reviewer_email.casefold() == operator_email.casefold()
    ):
        raise CliInputError(
            "Reviewer and operator email addresses must identify different accounts."
        )


def _confirmed_password(
    label: str,
    password_reader: Callable[[str], str],
) -> str:
    first = password_reader(f"{label} password: ")
    second = password_reader(f"Confirm {label.lower()} password: ")
    if first != second:
        raise CliInputError(f"{label} password confirmation does not match.")
    if not first:
        raise CliInputError(f"{label} password must not be empty.")
    return first


def _new_client(api_url: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"{api_url.rstrip('/')}/",
        timeout=_REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
        headers={
            "Accept": "application/json",
            "User-Agent": "MagicForge-Governance-Provisioner/1.0",
        },
    )


def _safe_response_error(response: httpx.Response) -> SafeApiError:
    api_code: str | None = None
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        body = None
    if isinstance(body, Mapping):
        detail = body.get("detail")
        if isinstance(detail, Mapping):
            candidate = detail.get("code")
            if isinstance(candidate, str) and _SAFE_API_CODE.fullmatch(candidate):
                api_code = candidate
    return SafeApiError(
        "http_error",
        status_code=response.status_code,
        api_code=api_code,
    )


def _post_json(
    client: httpx.Client,
    path: str,
    payload: Mapping[str, Any],
    *,
    token: str | None = None,
    expected_status: int,
) -> Mapping[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        response = client.post(
            path.lstrip("/"), json=dict(payload), headers=headers
        )
    except httpx.TimeoutException as exc:
        raise SafeApiError("request_timeout") from exc
    except httpx.HTTPError as exc:
        raise SafeApiError("request_failed") from exc
    if response.status_code != expected_status:
        raise _safe_response_error(response)
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise SafeApiError("invalid_api_response") from exc
    if not isinstance(body, Mapping):
        raise SafeApiError("invalid_api_response")
    return body


def _account_payload(
    *,
    username: str,
    email: str | None,
    password: str,
    role: str,
) -> dict[str, Any]:
    return {
        "username": username,
        "email": email,
        "password": password,
        "roles": [role],
        "reason": (
            f"Explicitly provision a distinct Production governance {role} "
            "through the local administration CLI."
        ),
    }


def _redacted_plan(
    *,
    api_url: str,
    admin_identifier: str,
    reviewer_username: str,
    reviewer_email: str | None,
    operator_username: str,
    operator_email: str | None,
) -> dict[str, Any]:
    return {
        "status": "dry_run",
        "api_url": api_url,
        "admin_identifier": admin_identifier,
        "accounts": [
            {
                "username": reviewer_username,
                "email": reviewer_email,
                "roles": ["reviewer"],
            },
            {
                "username": operator_username,
                "email": operator_email,
                "roles": ["operator"],
            },
        ],
        "requests_sent": 0,
    }


def _created_account(body: Mapping[str, Any], expected_role: str) -> dict[str, Any]:
    account_id = body.get("id")
    username = body.get("username")
    roles = body.get("roles")
    if (
        not isinstance(account_id, str)
        or not isinstance(username, str)
        or not isinstance(roles, list)
        or roles != [expected_role]
    ):
        raise SafeApiError("invalid_api_response")
    return {
        "status": "created",
        "id": account_id,
        "username": username,
        "roles": roles,
    }


def _provision(
    client: httpx.Client,
    *,
    admin_identifier: str,
    admin_password: str,
    reviewer_username: str,
    reviewer_email: str | None,
    reviewer_password: str,
    operator_username: str,
    operator_email: str | None,
    operator_password: str,
) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {
        "status": "failed",
        "reviewer": {"status": "not_attempted"},
        "operator": {"status": "not_attempted"},
        "logout": {"status": "not_attempted"},
    }
    token: str | None = None
    try:
        try:
            login = _post_json(
                client,
                "/auth/login",
                {
                    "identifier": admin_identifier,
                    "password": admin_password,
                    "transport": "bearer",
                    "device_label": "governance-account-cli",
                },
                expected_status=200,
            )
            candidate_token = login.get("access_token")
            if (
                login.get("transport") != "bearer"
                or not isinstance(candidate_token, str)
                or not candidate_token
            ):
                raise SafeApiError("invalid_api_response")
            token = candidate_token
        except SafeApiError as exc:
            report["login"] = {"status": "failed", "error": exc.as_dict()}
            return 1, report

        report["login"] = {"status": "succeeded"}
        account_requests = (
            (
                "reviewer",
                _account_payload(
                    username=reviewer_username,
                    email=reviewer_email,
                    password=reviewer_password,
                    role="reviewer",
                ),
            ),
            (
                "operator",
                _account_payload(
                    username=operator_username,
                    email=operator_email,
                    password=operator_password,
                    role="operator",
                ),
            ),
        )
        for role, payload in account_requests:
            try:
                body = _post_json(
                    client,
                    "/admin/users",
                    payload,
                    token=token,
                    expected_status=201,
                )
                report[role] = _created_account(body, role)
            except SafeApiError as exc:
                report[role] = {"status": "failed", "error": exc.as_dict()}
            finally:
                payload["password"] = ""
    finally:
        admin_password = ""
        reviewer_password = ""
        operator_password = ""
        if token is not None:
            try:
                logout = _post_json(
                    client,
                    "/auth/logout",
                    {},
                    token=token,
                    expected_status=200,
                )
                revoked = logout.get("revoked")
                if not isinstance(revoked, bool):
                    raise SafeApiError("invalid_api_response")
                report["logout"] = {
                    "status": "succeeded",
                    "revoked": revoked,
                }
            except SafeApiError as exc:
                report["logout"] = {
                    "status": "failed",
                    "error": exc.as_dict(),
                }
            token = ""

    account_statuses = {report[role]["status"] for role in ("reviewer", "operator")}
    logout_succeeded = report["logout"]["status"] == "succeeded"
    if account_statuses == {"created"} and logout_succeeded:
        report["status"] = "succeeded"
        return 0, report
    if "created" in account_statuses:
        report["status"] = "partial_failure"
    else:
        report["status"] = "failed"
    return 1, report


def _human_lines(report: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    output: list[str] = []
    errors: list[str] = []
    status = report.get("status")
    if status == "dry_run":
        output.append("Dry run: no API requests were sent.")
        for account in report["accounts"]:
            output.append(
                f"Would provision {account['username']!r} as {account['roles'][0]}."
            )
        return output, errors

    for role in ("reviewer", "operator"):
        account = report.get(role)
        if not isinstance(account, Mapping):
            continue
        if account.get("status") == "created":
            output.append(
                f"Created {role} {account['username']!r} ({account['id']})."
            )
        elif account.get("status") == "failed":
            error = account.get("error", {})
            detail = _human_error_detail(error)
            errors.append(f"Failed to create {role}: {detail}")

    login = report.get("login")
    if isinstance(login, Mapping) and login.get("status") == "failed":
        detail = _human_error_detail(login.get("error", {}))
        errors.append(f"Administrator login failed: {detail}")
    logout = report.get("logout")
    if isinstance(logout, Mapping) and logout.get("status") == "failed":
        errors.append(
            "Administrator logout failed: "
            f"{_human_error_detail(logout.get('error', {}))}"
        )
    elif isinstance(logout, Mapping) and logout.get("status") == "succeeded":
        output.append("Administrator API session logout completed.")
    return output, errors


def _human_error_detail(value: object) -> str:
    if not isinstance(value, Mapping):
        return "safe diagnostic unavailable."
    code = value.get("code", "request_failed")
    status = value.get("status_code")
    api_code = value.get("api_code")
    parts = [str(code)]
    if isinstance(status, int):
        parts.append(f"HTTP {status}")
    if isinstance(api_code, str):
        parts.append(f"API code {api_code}")
    return ", ".join(parts) + "."


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        api_url = _normalize_api_url(arguments.api_url)
        admin_identifier = _required_text(
            arguments.admin_identifier, "Administrator identifier"
        )
        reviewer_username = _required_text(
            arguments.reviewer_username, "Reviewer username"
        )
        reviewer_email = _optional_text(arguments.reviewer_email)
        operator_username = _required_text(
            arguments.operator_username, "Operator username"
        )
        operator_email = _optional_text(arguments.operator_email)
        _validate_distinct_accounts(
            reviewer_username,
            reviewer_email,
            operator_username,
            operator_email,
        )
    except CliInputError as exc:
        if arguments.json_output:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error": {"code": "invalid_input", "message": str(exc)},
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(f"Provisioning failed: {exc}", file=sys.stderr)
        return 2

    if arguments.dry_run:
        report = _redacted_plan(
            api_url=api_url,
            admin_identifier=admin_identifier,
            reviewer_username=reviewer_username,
            reviewer_email=reviewer_email,
            operator_username=operator_username,
            operator_email=operator_email,
        )
        exit_code = 0
    else:
        admin_password = ""
        reviewer_password = ""
        operator_password = ""
        try:
            admin_password = getpass.getpass("Administrator password: ")
            if not admin_password:
                raise CliInputError("Administrator password must not be empty.")
            reviewer_password = _confirmed_password("Reviewer", getpass.getpass)
            operator_password = _confirmed_password("Operator", getpass.getpass)
            with _new_client(api_url) as client:
                exit_code, report = _provision(
                    client,
                    admin_identifier=admin_identifier,
                    admin_password=admin_password,
                    reviewer_username=reviewer_username,
                    reviewer_email=reviewer_email,
                    reviewer_password=reviewer_password,
                    operator_username=operator_username,
                    operator_email=operator_email,
                    operator_password=operator_password,
                )
        except CliInputError as exc:
            report = {
                "status": "failed",
                "error": {"code": "invalid_input", "message": str(exc)},
            }
            exit_code = 2
        finally:
            admin_password = ""
            reviewer_password = ""
            operator_password = ""

    if arguments.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        output, errors = _human_lines(report)
        for line in output:
            print(line)
        for line in errors:
            print(line, file=sys.stderr)
        if not output and not errors and report.get("error"):
            print(
                f"Provisioning failed: {report['error']['message']}",
                file=sys.stderr,
            )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
