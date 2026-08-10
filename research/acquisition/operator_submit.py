"""Submit one content-addressed generic acquisition ledger to Source review.

This command is deliberately dry-run by default.  Its only write surface is
``POST /sources`` and execution requires the full ledger SHA-256 to be supplied
twice.  It never approves a Source, starts extraction, calls an LLM, creates a
Claim, writes a Manifest, or contacts Qdrant.

The operator password is read from ``MAGICFORGE_OPERATOR_PASSWORD`` or a hidden
terminal prompt.  Authentication uses a revocable cookie session plus the
server's double-submit CSRF token; neither value is written to disk or output.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from pydantic import ValidationError

from research.acquisition.generic_staging import (
    LEDGER_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    verify_generic_staging,
)
from research.acquisition.staging import AcquisitionStagingError
from research.review.workflow_models import SourceSummary, SourceVersionCommand
from security.governance_account_cli import (
    CliInputError,
    SafeApiError,
    _human_error_detail,
    _is_loopback,
    _normalize_api_url,
    _safe_response_error,
)


_DEFAULT_API_URL = "http://127.0.0.1:8000"
_DEFAULT_CSRF_COOKIE_NAME = "magicforge_csrf"
_REQUEST_TIMEOUT_SECONDS = 30.0
_MAX_ARTIFACT_BYTES = 12_500_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,254}$")
_EXPECTED_LEDGER_CONSTRAINTS: dict[str, object] = {
    "network_access": False,
    "database_write": False,
    "api_submission": False,
    "approval_performed": False,
    "human_review_decision": False,
    "semantic_extraction": False,
    "qdrant_write": False,
    "candidate_snippet_is_source_content": False,
    "requested_extraction_permission": "none",
    "requested_storage_permission": "none",
}
_WRAPPER_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "candidate_id",
        "identity",
        "content_sha256",
        "idempotency_key",
        "payload",
    }
)


class GenericSubmissionPreflightError(ValueError):
    """Fail-closed artifact error with a stable, content-free code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedGenericSource:
    """A verified Source command; Source text is never rendered in reports."""

    payload_file: str
    candidate_id: UUID
    expected_source_id: UUID
    canonical_key: str
    content_sha256: str
    idempotency_key: str
    command: SourceVersionCommand
    payload: Mapping[str, Any]

    def dry_run_record(self) -> dict[str, Any]:
        return {
            "payload_file": self.payload_file,
            "candidate_id": str(self.candidate_id),
            "expected_source_id": str(self.expected_source_id),
            "content_sha256": self.content_sha256,
            "status": "ready",
        }


@dataclass(frozen=True, slots=True)
class GenericLedgerBatch:
    ledger_sha256: str
    prepared_count: int
    blocked_count: int

    def report_record(self) -> dict[str, Any]:
        return {
            "ledger_sha256": self.ledger_sha256,
            "prepared_count": self.prepared_count,
            "blocked_count": self.blocked_count,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m research.acquisition.operator_submit",
        description=(
            "Verify one generic acquisition ledger and, only with --execute, "
            "register its Sources for later independent human review."
        ),
    )
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--ledger-sha256", required=True)
    parser.add_argument("--api-url", default=_DEFAULT_API_URL)
    parser.add_argument("--operator-identifier")
    parser.add_argument(
        "--csrf-cookie-name",
        default=None,
        help=(
            "Readable CSRF cookie name. Defaults to MAGICFORGE_CSRF_COOKIE_NAME, "
            "AUTH_CSRF_COOKIE_NAME, then magicforge_csrf."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Authenticate and POST /sources. Omit for an offline dry-run.",
    )
    parser.add_argument(
        "--confirm-ledger-sha256",
        help=(
            "Required with --execute and must exactly repeat --ledger-sha256; "
            "this prevents accidental submission of an obsolete ledger."
        ),
    )
    parser.add_argument("--json", dest="json_output", action="store_true")
    return parser


def _new_client(api_url: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"{api_url.rstrip('/')}/",
        timeout=_REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
        headers={
            "Accept": "application/json",
            "User-Agent": "MagicForge-Generic-Source-Submitter/1.0",
        },
    )


def _normalize_local_api_url(value: str) -> str:
    normalized = _normalize_api_url(value)
    hostname = urlsplit(normalized).hostname
    if hostname is None or not _is_loopback(hostname):
        raise CliInputError("Generic Source submission is limited to a local API.")
    return normalized


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey(key)
        value[key] = item
    return value


def _reject_non_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _read_bounded(path: Path, code: str) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise GenericSubmissionPreflightError(code)
        size = path.stat().st_size
        if size > _MAX_ARTIFACT_BYTES:
            raise GenericSubmissionPreflightError("artifact_too_large")
        return path.read_bytes()
    except GenericSubmissionPreflightError:
        raise
    except OSError as exc:
        raise GenericSubmissionPreflightError(code) from exc


def _load_json_object(raw: bytes, code: str) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_non_json_constant,
        )
    except _DuplicateJsonKey as exc:
        raise GenericSubmissionPreflightError("duplicate_json_key") from exc
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise GenericSubmissionPreflightError(code) from exc
    if not isinstance(value, dict):
        raise GenericSubmissionPreflightError(code)
    return value


def _canonical_hash(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise GenericSubmissionPreflightError("ledger_not_canonicalizable") from exc
    return hashlib.sha256(encoded).hexdigest()


def _valid_ledger_address(path: Path) -> str | None:
    """Return a self-consistent address without trusting malformed neighbors."""

    match = re.fullmatch(r"([0-9a-f]{64})\.json", path.name)
    if match is None:
        return None
    try:
        document = _load_json_object(
            _read_bounded(path, "ledger_unreadable"), "ledger_invalid"
        )
        recorded = document.pop("ledger_sha256", None)
        address = match.group(1)
        if recorded == address and _canonical_hash(document) == address:
            return address
    except GenericSubmissionPreflightError:
        return None
    return None


def _reject_superseded_ledger(root: Path, selected: Path) -> None:
    """Prevent submission from an older valid ledger in the same staging set.

    Generic staging keeps immutable history.  File creation time is therefore
    a safe operational ordering signal inside one staging directory; an exact
    second hash confirmation remains required for execution as a separate
    human-intent check.
    """

    try:
        selected_time = selected.stat().st_mtime_ns
        neighbors = list((root / "ledgers").glob("*.json"))
    except OSError as exc:
        raise GenericSubmissionPreflightError("ledger_currentness_unavailable") from exc
    for neighbor in neighbors:
        if neighbor == selected or _valid_ledger_address(neighbor) is None:
            continue
        try:
            neighbor_time = neighbor.stat().st_mtime_ns
        except OSError as exc:
            raise GenericSubmissionPreflightError(
                "ledger_currentness_unavailable"
            ) from exc
        if neighbor_time > selected_time:
            raise GenericSubmissionPreflightError("ledger_superseded")
        if neighbor_time == selected_time:
            raise GenericSubmissionPreflightError("ledger_currentness_ambiguous")


def _safe_payload_path(root: Path, relative_value: object) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise GenericSubmissionPreflightError("payload_path_invalid")
    relative = Path(relative_value)
    if relative.is_absolute():
        raise GenericSubmissionPreflightError("payload_path_invalid")
    path = (root / relative).resolve()
    try:
        path.relative_to((root / "source_payloads").resolve())
    except ValueError as exc:
        raise GenericSubmissionPreflightError("payload_path_invalid") from exc
    return path


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise GenericSubmissionPreflightError("source_identity_invalid")
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path or "/",
            parsed.query,
            "",
        )
    ).rstrip("/")


def _source_identity(command: SourceVersionCommand) -> tuple[str, str, UUID]:
    if command.citation.doi:
        identity = command.citation.doi
        canonical_key = f"doi:{identity}"
    else:
        identity = _canonical_url(command.citation.url)
        canonical_key = (
            "url-sha256:"
            f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"
        )
    return (
        identity,
        canonical_key,
        uuid5(NAMESPACE_URL, f"magicforge:research:{identity}"),
    )


def _prepare_payload(
    root: Path,
    entry: Mapping[str, Any],
) -> PreparedGenericSource:
    submission = entry.get("source_submission")
    exact = entry.get("exact_content")
    if not isinstance(submission, Mapping) or not isinstance(exact, Mapping):
        raise GenericSubmissionPreflightError("ledger_entry_invalid")
    if (
        submission.get("status") != "prepared"
        or submission.get("blocked_reasons") != []
        or submission.get("requested_extraction_permission") != "none"
        or submission.get("requested_storage_permission") != "none"
        or submission.get("citation_verification") != []
        or exact.get("status") != "matched"
    ):
        raise GenericSubmissionPreflightError("ledger_submission_policy_mismatch")
    path = _safe_payload_path(root, submission.get("payload_file"))
    raw = _read_bounded(path, "source_payload_unreadable")
    payload_sha256 = submission.get("payload_sha256")
    if (
        not isinstance(payload_sha256, str)
        or not _SHA256.fullmatch(payload_sha256)
        or hashlib.sha256(raw).hexdigest() != payload_sha256
    ):
        raise GenericSubmissionPreflightError("source_payload_hash_mismatch")
    wrapper = _load_json_object(raw, "source_payload_invalid")
    if frozenset(wrapper) != _WRAPPER_FIELDS:
        raise GenericSubmissionPreflightError("source_payload_wrapper_invalid")
    if (
        wrapper.get("schema_version") != "generic-source-submission-dry-run-0.1"
        or wrapper.get("status") != "dry_run_not_submitted"
    ):
        raise GenericSubmissionPreflightError("source_payload_schema_invalid")

    candidate_text = entry.get("candidate_id")
    if not isinstance(candidate_text, str) or wrapper.get("candidate_id") != candidate_text:
        raise GenericSubmissionPreflightError("candidate_binding_mismatch")
    try:
        candidate_id = UUID(candidate_text)
    except ValueError as exc:
        raise GenericSubmissionPreflightError("candidate_id_invalid") from exc
    if wrapper.get("identity") != entry.get("identity"):
        raise GenericSubmissionPreflightError("identity_binding_mismatch")

    content_sha256 = wrapper.get("content_sha256")
    if (
        not isinstance(content_sha256, str)
        or not _SHA256.fullmatch(content_sha256)
        or content_sha256 != exact.get("content_sha256")
    ):
        raise GenericSubmissionPreflightError("content_binding_mismatch")
    idempotency_key = wrapper.get("idempotency_key")
    expected_idempotency_key = f"generic-source:{candidate_id}:{content_sha256}"
    if (
        not isinstance(idempotency_key, str)
        or not _SAFE_IDEMPOTENCY_KEY.fullmatch(idempotency_key)
        or idempotency_key != expected_idempotency_key
    ):
        raise GenericSubmissionPreflightError("idempotency_binding_mismatch")
    payload = wrapper.get("payload")
    if not isinstance(payload, dict):
        raise GenericSubmissionPreflightError("source_command_invalid")
    try:
        command = SourceVersionCommand.model_validate(payload)
    except (ValidationError, RecursionError) as exc:
        # Validation errors may echo Source text; only the stable code escapes.
        raise GenericSubmissionPreflightError("source_command_invalid") from exc
    if (
        command.requested_extraction_permission.value != "none"
        or command.requested_storage_permission.value != "none"
        or command.requested_scope_locators
        or command.citation_verification
    ):
        raise GenericSubmissionPreflightError("governance_boundary_violated")
    if command.sensitive_information_level.value != "controlled":
        raise GenericSubmissionPreflightError("sensitivity_not_controlled")
    if command.access.redistribution_allowed is not False:
        raise GenericSubmissionPreflightError("redistribution_must_be_false")
    computed_content_hash = hashlib.sha256(command.content.encode("utf-8")).hexdigest()
    if computed_content_hash != content_sha256:
        raise GenericSubmissionPreflightError("content_hash_mismatch")

    identity, canonical_key, expected_source_id = _source_identity(command)
    ledger_identity = entry.get("identity")
    if not isinstance(ledger_identity, Mapping):
        raise GenericSubmissionPreflightError("identity_binding_mismatch")
    if (
        ledger_identity.get("canonical_value") != identity
        or ledger_identity.get("key")
        not in {f"doi:{identity}", f"url:{identity}"}
        or expected_source_id != candidate_id
    ):
        raise GenericSubmissionPreflightError("source_identity_mismatch")
    return PreparedGenericSource(
        payload_file=str(path.relative_to(root)),
        candidate_id=candidate_id,
        expected_source_id=expected_source_id,
        canonical_key=canonical_key,
        content_sha256=content_sha256,
        idempotency_key=idempotency_key,
        command=command,
        payload=payload,
    )


def preflight_generic_ledger(
    staging_dir: Path,
    ledger_sha256: str,
) -> tuple[list[PreparedGenericSource], GenericLedgerBatch]:
    """Revalidate a hash-addressed ledger and every referenced Source payload."""

    if not _SHA256.fullmatch(ledger_sha256):
        raise GenericSubmissionPreflightError("ledger_hash_invalid")
    try:
        root = Path(staging_dir).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CliInputError("Staging directory does not exist.") from exc
    if not root.is_dir():
        raise CliInputError("Staging path must be a directory.")
    try:
        verification = verify_generic_staging(root, ledger_sha256)
    except AcquisitionStagingError as exc:
        code = (
            exc.code
            if isinstance(exc.code, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", exc.code)
            else "generic_staging_verification_failed"
        )
        raise GenericSubmissionPreflightError(code) from exc

    ledger_path = root / "ledgers" / f"{ledger_sha256}.json"
    ledger = _load_json_object(
        _read_bounded(ledger_path, "ledger_unreadable"), "ledger_invalid"
    )
    recorded_hash = ledger.pop("ledger_sha256", None)
    if recorded_hash != ledger_sha256 or _canonical_hash(ledger) != ledger_sha256:
        raise GenericSubmissionPreflightError("ledger_hash_mismatch")
    _reject_superseded_ledger(root, ledger_path)
    if (
        ledger.get("schema_version") != LEDGER_SCHEMA_VERSION
        or ledger.get("mode") != "dry_run"
        or ledger.get("constraints") != _EXPECTED_LEDGER_CONSTRAINTS
    ):
        raise GenericSubmissionPreflightError("ledger_policy_mismatch")

    report_path = root / "reports" / f"{ledger_sha256}.json"
    report = _load_json_object(
        _read_bounded(report_path, "ledger_report_unreadable"),
        "ledger_report_invalid",
    )
    if (
        report.get("schema_version") != REPORT_SCHEMA_VERSION
        or report.get("status") != "dry_run"
        or report.get("ledger_sha256") != ledger_sha256
        or report.get("summary") != ledger.get("summary")
        or report.get("safety")
        != {
            "network_called": False,
            "database_modified": False,
            "approval_created": False,
            "qdrant_modified": False,
        }
    ):
        raise GenericSubmissionPreflightError("ledger_report_mismatch")

    entries = ledger.get("entries")
    summary = ledger.get("summary")
    if not isinstance(entries, list) or not isinstance(summary, Mapping):
        raise GenericSubmissionPreflightError("ledger_entries_invalid")
    prepared: list[PreparedGenericSource] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise GenericSubmissionPreflightError("ledger_entry_invalid")
        submission = entry.get("source_submission")
        if not isinstance(submission, Mapping):
            raise GenericSubmissionPreflightError("ledger_entry_invalid")
        status = submission.get("status")
        if status == "prepared":
            prepared.append(_prepare_payload(root, entry))
        elif status != "blocked":
            raise GenericSubmissionPreflightError("submission_status_invalid")

    prepared_count = summary.get("source_payloads_prepared")
    blocked_count = summary.get("blocked_candidates")
    if (
        not isinstance(prepared_count, int)
        or not isinstance(blocked_count, int)
        or prepared_count != len(prepared)
        or verification
        != {
            "ledger_verified": 1,
            "source_payloads_verified": len(prepared),
        }
    ):
        raise GenericSubmissionPreflightError("ledger_summary_mismatch")
    if not prepared:
        raise GenericSubmissionPreflightError("ledger_has_no_prepared_sources")

    for attribute, code in (
        ("candidate_id", "duplicate_candidate_id"),
        ("expected_source_id", "duplicate_source_id"),
        ("canonical_key", "duplicate_source_identity"),
        ("idempotency_key", "duplicate_idempotency_key"),
        ("payload_file", "duplicate_payload_file"),
    ):
        values = [getattr(item, attribute) for item in prepared]
        if len(values) != len(set(values)):
            raise GenericSubmissionPreflightError(code)
    return prepared, GenericLedgerBatch(
        ledger_sha256=ledger_sha256,
        prepared_count=len(prepared),
        blocked_count=blocked_count,
    )


def _origin(api_url: str) -> str:
    parsed = urlsplit(api_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _post_cookie_json(
    client: httpx.Client,
    path: str,
    payload: Mapping[str, Any],
    *,
    origin: str,
    csrf_token: str | None = None,
    idempotency_key: str | None = None,
    expected_status: int,
    allow_replay_status: bool = False,
) -> tuple[Mapping[str, Any], bool]:
    headers = {"Origin": origin}
    if csrf_token is not None:
        headers["X-CSRF-Token"] = csrf_token
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    try:
        response = client.post(path.lstrip("/"), json=dict(payload), headers=headers)
    except httpx.TimeoutException as exc:
        raise SafeApiError("request_timeout") from exc
    except httpx.HTTPError as exc:
        raise SafeApiError("request_failed") from exc
    replay = False
    if response.status_code != expected_status:
        if (
            allow_replay_status
            and response.status_code == 200
            and response.headers.get("Idempotency-Replayed", "").casefold() == "true"
        ):
            replay = True
        else:
            raise _safe_response_error(response)
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise SafeApiError("invalid_api_response") from exc
    if not isinstance(body, Mapping):
        raise SafeApiError("invalid_api_response")
    return body, replay


def _csrf_cookie(client: httpx.Client, name: str) -> str:
    matches = [cookie.value for cookie in client.cookies.jar if cookie.name == name]
    if len(matches) != 1 or not matches[0]:
        raise SafeApiError("csrf_cookie_unavailable")
    return matches[0]


def _cookie_login(
    client: httpx.Client,
    *,
    api_origin: str,
    identifier: str,
    password: str,
    csrf_cookie_name: str,
) -> str:
    body, _replay = _post_cookie_json(
        client,
        "/auth/login",
        {
            "identifier": identifier,
            "password": password,
            "transport": "cookie",
            "device_label": "generic-source-submitter",
        },
        origin=api_origin,
        expected_status=200,
    )
    try:
        csrf = _csrf_cookie(client, csrf_cookie_name)
    except SafeApiError as exc:
        exc.csrf_token = None
        raise
    actor = body.get("actor")
    roles = actor.get("roles") if isinstance(actor, Mapping) else None
    if (
        body.get("transport") != "cookie"
        or body.get("csrf_required") is not True
        or body.get("access_token") is not None
        or roles != ["operator"]
    ):
        error = SafeApiError("operator_cookie_login_response_invalid")
        error.csrf_token = csrf
        raise error
    return csrf


def _validate_source_response(
    body: Mapping[str, Any],
    submission: PreparedGenericSource,
) -> dict[str, Any]:
    try:
        summary = SourceSummary.model_validate(body)
    except ValidationError as exc:
        raise SafeApiError("invalid_source_response") from exc
    if (
        summary.id != submission.expected_source_id
        or summary.canonical_key != submission.canonical_key
        or summary.title != submission.command.citation.title
        or summary.source_category != submission.command.source_category
    ):
        raise SafeApiError("source_response_identity_mismatch")
    matching = [
        item
        for item in summary.versions
        if item.content_hash == submission.content_sha256
    ]
    if len(summary.versions) != 1 or len(matching) != 1:
        raise SafeApiError("source_response_version_mismatch")
    version = matching[0]
    expected_version_id = uuid5(
        NAMESPACE_URL,
        f"magicforge:source-version:{summary.id}:{submission.content_sha256}",
    )
    if (
        version.id != expected_version_id
        or version.version != 1
        or version.status.value != "submitted"
        or version.citation_status != "unverified"
        or version.sensitivity.value != "controlled"
        or version.content_access != submission.command.content_access
        or version.source_type != submission.command.source_type
        or version.knowledge_origin != submission.command.knowledge_origin
    ):
        raise SafeApiError("source_response_version_mismatch")
    return {
        "source_id": str(summary.id),
        "source_version_id": str(version.id),
        "version": version.version,
        "content_sha256": version.content_hash,
        "workflow_status": version.status.value,
    }


def _execute_submissions(
    client: httpx.Client,
    *,
    api_url: str,
    identifier: str,
    password: str,
    csrf_cookie_name: str,
    submissions: Sequence[PreparedGenericSource],
    batch: GenericLedgerBatch,
) -> tuple[int, dict[str, Any]]:
    report: dict[str, Any] = {
        "status": "failed",
        "mode": "execute",
        "batch": batch.report_record(),
        "preflight": {"status": "passed", "payload_count": len(submissions)},
        "login": {"status": "not_attempted"},
        "submissions": [item.dry_run_record() for item in submissions],
        "logout": {"status": "not_attempted"},
        "source_requests_sent": 0,
    }
    api_origin = _origin(api_url)
    csrf: str | None = None
    try:
        try:
            csrf = _cookie_login(
                client,
                api_origin=api_origin,
                identifier=identifier,
                password=password,
                csrf_cookie_name=csrf_cookie_name,
            )
        except SafeApiError as exc:
            csrf = getattr(exc, "csrf_token", None)
            report["login"] = {"status": "failed", "error": exc.as_dict()}
            return 1, report
        report["login"] = {
            "status": "succeeded",
            "transport": "cookie",
            "roles": ["operator"],
        }

        for index, submission in enumerate(submissions):
            report["source_requests_sent"] += 1
            try:
                body, replay = _post_cookie_json(
                    client,
                    "/sources",
                    submission.payload,
                    origin=api_origin,
                    csrf_token=csrf,
                    idempotency_key=submission.idempotency_key,
                    expected_status=201,
                    allow_replay_status=True,
                )
                result = _validate_source_response(body, submission)
                report["submissions"][index] = {
                    "payload_file": submission.payload_file,
                    "candidate_id": str(submission.candidate_id),
                    "status": (
                        "idempotent_replay"
                        if replay
                        else "accepted_or_idempotent_replay"
                    ),
                    **result,
                }
            except SafeApiError as exc:
                report["submissions"][index] = {
                    "payload_file": submission.payload_file,
                    "candidate_id": str(submission.candidate_id),
                    "status": "failed",
                    "error": exc.as_dict(),
                }
    finally:
        password = ""
        if csrf is not None:
            try:
                body, _replay = _post_cookie_json(
                    client,
                    "/auth/logout",
                    {},
                    origin=api_origin,
                    csrf_token=csrf,
                    expected_status=200,
                )
                if body.get("revoked") is not True:
                    raise SafeApiError("logout_not_revoked")
                report["logout"] = {"status": "succeeded", "revoked": True}
            except SafeApiError as exc:
                report["logout"] = {"status": "failed", "error": exc.as_dict()}
            csrf = ""

    accepted_statuses = {"accepted_or_idempotent_replay", "idempotent_replay"}
    statuses = {item["status"] for item in report["submissions"]}
    if statuses and statuses <= accepted_statuses and report["logout"]["status"] == "succeeded":
        report["status"] = "succeeded"
        return 0, report
    if statuses & accepted_statuses:
        report["status"] = "partial_failure"
    return 1, report


def _dry_run_report(
    api_url: str,
    submissions: Sequence[PreparedGenericSource],
    batch: GenericLedgerBatch,
) -> dict[str, Any]:
    return {
        "status": "dry_run",
        "mode": "dry_run",
        "api_url": api_url,
        "batch": batch.report_record(),
        "preflight": {"status": "passed", "payload_count": len(submissions)},
        "submissions": [item.dry_run_record() for item in submissions],
        "source_requests_sent": 0,
    }


def _credential(
    explicit_identifier: str | None,
    *,
    environ: Mapping[str, str],
    input_reader: Callable[[str], str],
    password_reader: Callable[[str], str],
) -> tuple[str, str]:
    try:
        identifier = (
            explicit_identifier
            or environ.get("MAGICFORGE_OPERATOR_IDENTIFIER")
            or input_reader("Operator username or email: ")
        ).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise CliInputError("Operator identifier input was cancelled.") from exc
    if not identifier:
        raise CliInputError("Operator identifier must not be empty.")
    password = environ.get("MAGICFORGE_OPERATOR_PASSWORD")
    if password is None:
        try:
            password = password_reader("Operator password: ")
        except (EOFError, KeyboardInterrupt) as exc:
            raise CliInputError("Operator password input was cancelled.") from exc
    if not password:
        raise CliInputError("Operator password must not be empty.")
    return identifier, password


def _csrf_name(value: str | None, environ: Mapping[str, str]) -> str:
    result = (
        value
        or environ.get("MAGICFORGE_CSRF_COOKIE_NAME")
        or environ.get("AUTH_CSRF_COOKIE_NAME")
        or _DEFAULT_CSRF_COOKIE_NAME
    ).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", result):
        raise CliInputError("CSRF cookie name is invalid.")
    return result


def _human_lines(report: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    output: list[str] = []
    errors: list[str] = []
    preflight = report.get("preflight")
    if isinstance(preflight, Mapping) and preflight.get("status") == "failed":
        errors.append(
            f"Preflight failed: {preflight.get('error', {}).get('code', 'invalid_artifact')}."
        )
        return output, errors
    if report.get("status") == "dry_run":
        count = report.get("preflight", {}).get("payload_count", 0)
        output.append(f"Dry run passed for {count} Source payload(s); no requests sent.")
        output.append(
            "Use --execute and repeat the exact ledger hash with "
            "--confirm-ledger-sha256 to register Sources only."
        )
        return output, errors
    login = report.get("login")
    if isinstance(login, Mapping) and login.get("status") == "failed":
        errors.append(
            f"Operator login failed: {_human_error_detail(login.get('error', {}))}"
        )
    for item in report.get("submissions", []):
        if not isinstance(item, Mapping):
            continue
        if item.get("status") in {
            "accepted_or_idempotent_replay",
            "idempotent_replay",
        }:
            output.append(
                f"{item['status']}: Source {item['source_id']} is submitted for review."
            )
        elif item.get("status") == "failed":
            errors.append(
                f"Source submission failed for {item.get('payload_file', 'artifact')}: "
                f"{_human_error_detail(item.get('error', {}))}"
            )
    logout = report.get("logout")
    if isinstance(logout, Mapping) and logout.get("status") == "succeeded":
        output.append("Operator cookie session logout completed.")
    elif isinstance(logout, Mapping) and logout.get("status") == "failed":
        errors.append(
            f"Operator logout failed: {_human_error_detail(logout.get('error', {}))}"
        )
    return output, errors


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    mode = "execute" if arguments.execute else "dry_run"
    try:
        api_url = _normalize_local_api_url(arguments.api_url)
        csrf_cookie_name = _csrf_name(arguments.csrf_cookie_name, os.environ)
        ledger_sha256 = arguments.ledger_sha256.strip().casefold()
        if arguments.execute:
            confirmation = str(arguments.confirm_ledger_sha256 or "").strip().casefold()
            if confirmation != ledger_sha256:
                raise CliInputError(
                    "Execution requires an exact repeated ledger SHA-256 confirmation."
                )
        submissions, batch = preflight_generic_ledger(
            arguments.staging_dir, ledger_sha256
        )
    except (CliInputError, GenericSubmissionPreflightError) as exc:
        code = exc.code if isinstance(exc, GenericSubmissionPreflightError) else "invalid_input"
        report: dict[str, Any] = {
            "status": "failed",
            "mode": mode,
            "preflight": {"status": "failed", "error": {"code": code}},
            "source_requests_sent": 0,
        }
        exit_code = 2
    else:
        if not arguments.execute:
            report = _dry_run_report(api_url, submissions, batch)
            exit_code = 0
        else:
            password = ""
            try:
                identifier, password = _credential(
                    arguments.operator_identifier,
                    environ=os.environ,
                    input_reader=input,
                    password_reader=getpass.getpass,
                )
                with _new_client(api_url) as client:
                    exit_code, report = _execute_submissions(
                        client,
                        api_url=api_url,
                        identifier=identifier,
                        password=password,
                        csrf_cookie_name=csrf_cookie_name,
                        submissions=submissions,
                        batch=batch,
                    )
            except CliInputError:
                report = {
                    "status": "failed",
                    "mode": mode,
                    "preflight": {
                        "status": "passed",
                        "payload_count": len(submissions),
                    },
                    "error": {"code": "invalid_credentials_input"},
                    "source_requests_sent": 0,
                }
                exit_code = 2
            finally:
                password = ""

    if arguments.json_output:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    else:
        output, errors = _human_lines(report)
        for line in output:
            print(line)
        for line in errors:
            print(line, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
