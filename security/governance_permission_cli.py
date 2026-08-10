"""Submit one narrow permission amendment for each official registered Source.

The command is offline and read-only by default.  Its only non-authentication
write surface is the append-only Source permission-request endpoint.  It
cannot review Sources, create Claims or mappings, build or authorize a
Manifest, authorize storage, ingest a Corpus, or contact Qdrant.

The official acquisition CLI remains the authority for artifact preflight.
This module calls that preflight before asking for credentials, then derives a
Source-version identity from each locked Source ID and content hash.  Source
content is never copied into a permission command or rendered in output.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from pydantic import ValidationError

from research.review.workflow_models import (
    SourcePermissionRequestCommand,
    SourcePermissionRequestView,
)
from security import governance_source_cli as source_cli
from security.governance_account_cli import (
    CliInputError,
    SafeApiError,
    _human_error_detail,
    _normalize_api_url,
)


_PERMISSION_RUN_ID = "production-source-permission-001"
_EXPECTED_SOURCE_COUNT = 29
_REQUEST_TIMEOUT_SECONDS = 30.0
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RIGHTS_BASIS = (
    "Internal Claim-extraction proposal from the exact registered web_extract "
    "only. It does not authorize redistribution of Source text, excerpts, or "
    "files. Independent human Source and Claim review remains required before "
    "any knowledge or storage authorization."
)
_REQUEST_REASON = (
    "Permission-ceiling amendment request only; this is not Source approval, "
    "Claim approval, extraction execution, storage authorization, Manifest "
    "authorization, ingestion, or Qdrant authorization. Independent human "
    "review is required."
)


@dataclass(frozen=True, slots=True)
class PreparedPermissionRequest:
    """Content-free material for one exact permission-request mutation."""

    file_name: str
    candidate_id: UUID
    source_id: UUID
    source_version_id: UUID
    content_sha256: str
    idempotency_key: str
    command: SourcePermissionRequestCommand

    def dry_run_record(self) -> dict[str, Any]:
        return {
            "file": self.file_name,
            "candidate_id": str(self.candidate_id),
            "source_id": str(self.source_id),
            "source_version_id": str(self.source_version_id),
            "content_sha256": self.content_sha256,
            "status": "ready",
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m security.governance_permission_cli",
        description=(
            "Preflight the official 29-Source acquisition batch and, only "
            "with --execute, submit append-only permission amendments."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    request = commands.add_parser(
        "request",
        help="Validate the exact request set; dry-run unless --execute is present.",
    )
    request.add_argument("--api-url", default="http://127.0.0.1:8000")
    request.add_argument("--operator-identifier", required=True)
    request.add_argument(
        "--payload-dir",
        type=Path,
        default=source_cli._DEFAULT_PAYLOAD_DIRECTORY,
        help=(
            "Official acquisition wrapper directory. Relocation is allowed "
            "only with its exact locked results bundle."
        ),
    )
    request.add_argument(
        "--results-file",
        type=Path,
        default=source_cli._DEFAULT_RESULTS_FILE,
        help="Official hash-bearing acquisition-results.json.",
    )
    request.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Log in and POST only append-only Source permission requests. "
            "Without this flag no HTTP client is created."
        ),
    )
    request.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Write one machine-readable, content-free result object.",
    )
    return parser


def _new_client(api_url: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"{api_url.rstrip('/')}/",
        timeout=_REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
        headers={
            "Accept": "application/json",
            "User-Agent": "MagicForge-Governance-Permission-Requester/1.0",
        },
    )


def _source_version_id(source_id: UUID, content_sha256: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"magicforge:source-version:{source_id}:{content_sha256}",
    )


def _rights_evidence(
    submission: source_cli.PreparedSourceSubmission,
) -> list[str]:
    command = submission.command
    evidence = [
        f"canonical_url={source_cli._canonical_url(command.citation.url)}",
    ]
    if command.citation.doi:
        evidence.append(f"doi={command.citation.doi}")
    evidence.extend(
        (
            f"content_sha256={submission.content_sha256}",
            f"content_access={command.content_access.value}",
            f"access_method={command.access.access_method}",
            "redistribution_allowed=false",
        )
    )
    return evidence


def _prepare_permission_request(
    submission: source_cli.PreparedSourceSubmission,
) -> PreparedPermissionRequest:
    """Derive a permission command without carrying Source content forward."""

    if submission.command.content_access.value != "web_extract":
        raise source_cli.PayloadPreflightError("content_access_not_web_extract")
    if submission.command.access.redistribution_allowed is not False:
        raise source_cli.PayloadPreflightError("redistribution_must_be_false")
    if not _SHA256.fullmatch(submission.content_sha256):
        raise source_cli.PayloadPreflightError("content_hash_invalid")

    version_id = _source_version_id(
        submission.expected_source_id,
        submission.content_sha256,
    )
    command = SourcePermissionRequestCommand(
        source_version_id=version_id,
        requested_extraction_permission="full_text",
        requested_storage_permission="derived_knowledge_only",
        requested_scope_locators=[],
        rights_basis=_RIGHTS_BASIS,
        rights_evidence=_rights_evidence(submission),
        reason=_REQUEST_REASON,
    )
    command_hash = source_cli._canonical_json_hash(
        command.model_dump(mode="json")
    )
    idempotency_key = (
        f"{_PERMISSION_RUN_ID}:{submission.expected_source_id}:{command_hash}"
    )
    if not source_cli._SAFE_IDEMPOTENCY_KEY.fullmatch(idempotency_key):
        raise source_cli.PayloadPreflightError("idempotency_key_invalid")
    return PreparedPermissionRequest(
        file_name=submission.file_name,
        candidate_id=submission.candidate_id,
        source_id=submission.expected_source_id,
        source_version_id=version_id,
        content_sha256=submission.content_sha256,
        idempotency_key=idempotency_key,
        command=command,
    )


def _duplicate_request_errors(
    prepared: Sequence[PreparedPermissionRequest],
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for attribute, code in (
        ("source_id", "duplicate_source_id"),
        ("source_version_id", "duplicate_source_version_id"),
        ("idempotency_key", "duplicate_idempotency_key"),
    ):
        seen: dict[object, str] = {}
        for item in prepared:
            value = getattr(item, attribute)
            previous = seen.get(value)
            if previous is not None:
                errors.extend(
                    (
                        {"file": previous, "code": code},
                        {"file": item.file_name, "code": code},
                    )
                )
            else:
                seen[value] = item.file_name
    unique = {(item["file"], item["code"]): item for item in errors}
    return list(unique.values())


def _preflight_official_permissions(
    payload_directory: Path,
    results_file: Path,
) -> tuple[
    list[PreparedPermissionRequest],
    list[dict[str, str]],
    source_cli.OfficialAcquisitionBatch | None,
]:
    """Call the official acquisition gate, then derive exactly 29 requests."""

    submissions, errors, batch = source_cli._preflight_directory(
        payload_directory,
        results_file,
    )
    if errors or batch is None:
        return [], errors, batch
    if (
        len(submissions) != _EXPECTED_SOURCE_COUNT
        or len(batch.expected_by_file) != _EXPECTED_SOURCE_COUNT
    ):
        return (
            [],
            [
                {
                    "file": "acquisition-results.json",
                    "code": "official_permission_source_count_mismatch",
                }
            ],
            batch,
        )

    prepared: list[PreparedPermissionRequest] = []
    permission_errors: list[dict[str, str]] = []
    for submission in submissions:
        try:
            prepared.append(_prepare_permission_request(submission))
        except (source_cli.PayloadPreflightError, ValidationError) as exc:
            code = (
                exc.code
                if isinstance(exc, source_cli.PayloadPreflightError)
                else "invalid_permission_request_command"
            )
            permission_errors.append({"file": submission.file_name, "code": code})
    permission_errors.extend(_duplicate_request_errors(prepared))
    return (
        prepared,
        sorted(permission_errors, key=lambda item: (item["file"], item["code"])),
        batch,
    )


def _post_json(
    client: httpx.Client,
    path: str,
    payload: Mapping[str, Any],
    *,
    token: str | None = None,
    idempotency_key: str | None = None,
    expected_status: int,
    allow_replay_status: bool = False,
) -> tuple[Mapping[str, Any], bool]:
    """Use the Source CLI's redacted response boundary."""

    return source_cli._post_json(
        client,
        path,
        payload,
        token=token,
        idempotency_key=idempotency_key,
        expected_status=expected_status,
        allow_replay_status=allow_replay_status,
    )


def _operator_login(
    client: httpx.Client,
    *,
    identifier: str,
    password: str,
) -> str:
    body, _replay = _post_json(
        client,
        "/auth/login",
        {
            "identifier": identifier,
            "password": password,
            "transport": "bearer",
            "device_label": "governance-permission-cli",
        },
        expected_status=200,
    )
    token = body.get("access_token")
    actor = body.get("actor")
    roles = actor.get("roles") if isinstance(actor, Mapping) else None
    if (
        body.get("transport") != "bearer"
        or not isinstance(token, str)
        or not token
        or roles != ["operator"]
    ):
        error = SafeApiError("operator_login_response_invalid")
        error.session_token = token if isinstance(token, str) and token else None
        raise error
    return token


def _validate_permission_response(
    body: Mapping[str, Any],
    prepared: PreparedPermissionRequest,
) -> dict[str, Any]:
    try:
        view = SourcePermissionRequestView.model_validate(body)
    except ValidationError as exc:
        raise SafeApiError("invalid_permission_request_response") from exc

    command = prepared.command
    if (
        view.source_version_id != prepared.source_version_id
        or view.sequence != 2
        or view.requested_extraction_permission
        != command.requested_extraction_permission
        or view.requested_storage_permission != command.requested_storage_permission
        or view.requested_scope_locators != command.requested_scope_locators
        or view.rights_basis != command.rights_basis
        or view.rights_evidence != command.rights_evidence
        or view.reason != command.reason
        or view.actor_role_snapshot != ["operator"]
        or not view.submitted_by.strip()
        or not _SHA256.fullmatch(view.request_checksum)
        or view.supersedes_request_id is None
    ):
        raise SafeApiError("permission_request_response_mismatch")
    return {
        "source_id": str(prepared.source_id),
        "source_version_id": str(view.source_version_id),
        "permission_request_id": str(view.id),
        "sequence": view.sequence,
        "request_checksum": view.request_checksum,
        "supersedes_request_id": str(view.supersedes_request_id),
    }


def _execute_requests(
    client: httpx.Client,
    *,
    operator_identifier: str,
    operator_password: str,
    requests: Sequence[PreparedPermissionRequest],
    batch: source_cli.OfficialAcquisitionBatch,
) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {
        "status": "failed",
        "mode": "execute",
        "permission_run_id": _PERMISSION_RUN_ID,
        "batch": batch.report_record(),
        "preflight": {"status": "passed", "request_count": len(requests)},
        "login": {"status": "not_attempted"},
        "requests": [
            {
                "file": item.file_name,
                "candidate_id": str(item.candidate_id),
                "source_id": str(item.source_id),
                "source_version_id": str(item.source_version_id),
                "status": "not_attempted",
            }
            for item in requests
        ],
        "logout": {"status": "not_attempted"},
        "permission_requests_sent": 0,
    }
    token: str | None = None
    try:
        try:
            token = _operator_login(
                client,
                identifier=operator_identifier,
                password=operator_password,
            )
        except SafeApiError as exc:
            token = getattr(exc, "session_token", None)
            report["login"] = {"status": "failed", "error": exc.as_dict()}
            return 1, report
        report["login"] = {"status": "succeeded", "roles": ["operator"]}

        for index, prepared in enumerate(requests):
            report["permission_requests_sent"] += 1
            path = (
                f"/sources/{prepared.source_id}/versions/"
                f"{prepared.source_version_id}/permission-requests"
            )
            try:
                body, replay = _post_json(
                    client,
                    path,
                    prepared.command.model_dump(mode="json"),
                    token=token,
                    idempotency_key=prepared.idempotency_key,
                    expected_status=201,
                    allow_replay_status=True,
                )
                result = _validate_permission_response(body, prepared)
                report["requests"][index] = {
                    "file": prepared.file_name,
                    "candidate_id": str(prepared.candidate_id),
                    "status": (
                        "idempotent_replay"
                        if replay
                        else "accepted_or_idempotent_replay"
                    ),
                    **result,
                }
            except SafeApiError as exc:
                report["requests"][index] = {
                    "file": prepared.file_name,
                    "candidate_id": str(prepared.candidate_id),
                    "source_id": str(prepared.source_id),
                    "source_version_id": str(prepared.source_version_id),
                    "status": "failed",
                    "error": exc.as_dict(),
                }
    finally:
        operator_password = ""
        if token is not None:
            try:
                body, _replay = _post_json(
                    client,
                    "/auth/logout",
                    {},
                    token=token,
                    expected_status=200,
                )
                if body.get("revoked") is not True:
                    raise SafeApiError("logout_not_revoked")
                report["logout"] = {
                    "status": "succeeded",
                    "revoked": True,
                }
            except SafeApiError as exc:
                report["logout"] = {
                    "status": "failed",
                    "error": exc.as_dict(),
                }
            token = ""

    accepted_statuses = {
        "accepted_or_idempotent_replay",
        "idempotent_replay",
    }
    statuses = {item["status"] for item in report["requests"]}
    accepted = bool(statuses) and statuses <= accepted_statuses
    logout_succeeded = report["logout"]["status"] == "succeeded"
    if accepted and logout_succeeded:
        report["status"] = "succeeded"
        return 0, report
    if statuses & accepted_statuses:
        report["status"] = "partial_failure"
    return 1, report


def _dry_run_report(
    api_url: str,
    operator_identifier: str,
    requests: Sequence[PreparedPermissionRequest],
    batch: source_cli.OfficialAcquisitionBatch,
) -> dict[str, Any]:
    return {
        "status": "dry_run",
        "mode": "dry_run",
        "permission_run_id": _PERMISSION_RUN_ID,
        "api_url": api_url,
        "operator_identifier": operator_identifier,
        "batch": batch.report_record(),
        "preflight": {"status": "passed", "request_count": len(requests)},
        "requests": [item.dry_run_record() for item in requests],
        "permission_requests_sent": 0,
    }


def _human_lines(report: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    output: list[str] = []
    errors: list[str] = []
    if report.get("status") == "dry_run":
        count = report.get("preflight", {}).get("request_count", 0)
        output.append(
            f"Dry run passed for {count} Source permission request(s); "
            "no requests sent."
        )
        output.append(
            "Re-run with --execute to submit only append-only permission requests."
        )
        return output, errors

    preflight = report.get("preflight")
    if isinstance(preflight, Mapping) and preflight.get("status") == "failed":
        for item in preflight.get("errors", []):
            if isinstance(item, Mapping):
                errors.append(
                    f"Preflight failed for {item.get('file', 'artifact')}: "
                    f"{item.get('code', 'invalid_artifact')}."
                )
        return output, errors

    login = report.get("login")
    if isinstance(login, Mapping) and login.get("status") == "failed":
        errors.append(
            "Operator login failed: "
            f"{_human_error_detail(login.get('error', {}))}"
        )
    for item in report.get("requests", []):
        if not isinstance(item, Mapping):
            continue
        if item.get("status") in {
            "accepted_or_idempotent_replay",
            "idempotent_replay",
        }:
            output.append(
                f"{item['status']}: Source {item['source_id']} permission "
                f"request sequence {item['sequence']}."
            )
        elif item.get("status") == "failed":
            errors.append(
                f"Permission request failed for {item.get('file', 'artifact')}: "
                f"{_human_error_detail(item.get('error', {}))}"
            )
    logout = report.get("logout")
    if isinstance(logout, Mapping) and logout.get("status") == "succeeded":
        output.append("Operator API session logout completed.")
    elif isinstance(logout, Mapping) and logout.get("status") == "failed":
        errors.append(
            "Operator logout failed: "
            f"{_human_error_detail(logout.get('error', {}))}"
        )
    return output, errors


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        api_url = _normalize_api_url(arguments.api_url)
        operator_identifier = arguments.operator_identifier.strip()
        if not operator_identifier:
            raise CliInputError("Operator identifier must not be empty.")
        requests, preflight_errors, batch = _preflight_official_permissions(
            arguments.payload_dir,
            arguments.results_file,
        )
    except CliInputError as exc:
        report: dict[str, Any] = {
            "status": "failed",
            "mode": "execute" if arguments.execute else "dry_run",
            "error": {"code": "invalid_input", "message": str(exc)},
            "permission_requests_sent": 0,
        }
        exit_code = 2
    else:
        if preflight_errors:
            report = {
                "status": "failed",
                "mode": "execute" if arguments.execute else "dry_run",
                "preflight": {
                    "status": "failed",
                    "request_count": len(requests),
                    "errors": preflight_errors,
                },
                "permission_requests_sent": 0,
            }
            exit_code = 2
        elif not arguments.execute:
            assert batch is not None
            report = _dry_run_report(
                api_url,
                operator_identifier,
                requests,
                batch,
            )
            exit_code = 0
        else:
            assert batch is not None
            operator_password = ""
            try:
                operator_password = getpass.getpass("Operator password: ")
                if not operator_password:
                    raise CliInputError("Operator password must not be empty.")
                with _new_client(api_url) as client:
                    exit_code, report = _execute_requests(
                        client,
                        operator_identifier=operator_identifier,
                        operator_password=operator_password,
                        requests=requests,
                        batch=batch,
                    )
            except CliInputError as exc:
                report = {
                    "status": "failed",
                    "mode": "execute",
                    "error": {"code": "invalid_input", "message": str(exc)},
                    "permission_requests_sent": 0,
                }
                exit_code = 2
            finally:
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
                f"Permission request command failed: {report['error']['message']}",
                file=sys.stderr,
            )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
