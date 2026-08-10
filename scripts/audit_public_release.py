#!/usr/bin/env python3
"""Fail-closed audit for an isolated MagicForge public-release tree.

The scanner intentionally reports only relative paths. It never follows a
symbolic link and never needs access to private corpus content outside the tree
being audited.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024
_RELEASE_MANIFEST_NAME = "PUBLIC_RELEASE_MANIFEST.json"
_RELEASE_MANIFEST_SCHEMA = "magicforge-public-release-manifest-1"

_CACHE_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".magicforge",
        ".next",
        ".playwright-cli",
        ".pytest_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)
_TOP_LEVEL_PRIVATE_DIRECTORIES = frozenset(
    {"output", "outputs", "snapshot", "snapshots", "storage"}
)
_PRIVATE_FILE_SUFFIXES = (
    ".db",
    ".dump",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    ".snapshot",
)
_PRIVATE_FILE_NAMES = frozenset(
    {
        ".env",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
    }
)
_GENERATED_FILE_SUFFIXES = (".pyc", ".pyo", ".tsbuildinfo")
_HOST_HOME_COMPONENTS = "(?:home|Users|root)"
_WINDOWS_USERS_COMPONENT = "Users"
_HOST_ABSOLUTE_PATHS = (
    re.compile(r"(?<![A-Za-z0-9:])/" + _HOST_HOME_COMPONENTS + r"/[^\s\"'`<>]+"),
    re.compile(r"(?<![A-Za-z0-9:])/mnt/[A-Za-z]/" + _WINDOWS_USERS_COMPONENT + r"/[^\s\"'`<>]+"),
    re.compile(r"\b[A-Za-z]:\\" + _WINDOWS_USERS_COMPONENT + r"\\[^\r\n\"'<>]+"),
)

# High-confidence provider and credential formats. Fragments are joined so the
# scanner source does not contain a token-shaped example that detects itself.
_TOKEN_PATTERNS = (
    ("private_key", re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?" + "PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("provider_token", re.compile(r"\b(?:sk|tvly)-[A-Za-z0-9_-]{20,}\b")),
    ("zai_token", re.compile(r"\b[0-9a-fA-F]{32}\.[A-Za-z0-9_-]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{20,}")),
)
_QUOTED_CREDENTIAL = re.compile(
    r"(?im)^\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*(?:api_?key|access_?token|auth_?token|"
    r"client_?secret|password|private_?key|secret|token))[ \t]*[:=][ \t]*"
    r"(?P<quote>[\"'])(?P<value>[^\r\n\"']+)(?P=quote)"
)
_BARE_CREDENTIAL = re.compile(
    r"(?m)^\s*(?:export\s+)?"
    r"(?P<key>[A-Z][A-Z0-9_]*(?:API_?KEY|ACCESS_?TOKEN|AUTH_?TOKEN|"
    r"CLIENT_?SECRET|PASSWORD|PRIVATE_?KEY|SECRET|TOKEN))[ \t]*=[ \t]*"
    r"(?P<value>[^\s#]+)"
)
_REDISTRIBUTION_FIELD = "redistribution_" + "allowed"
_REDISTRIBUTION_DENIAL = re.compile(
    rf"(?i)(?:[\"']?{_REDISTRIBUTION_FIELD}[\"']?)\s*[:=]\s*(?:false|no|0)\b"
)
_REDISTRIBUTION_EXTENSIONS = frozenset(
    {".json", ".jsonl", ".toml", ".yaml", ".yml"}
)
_PRIVATE_REFERENCE = re.compile(r"(?<![A-Za-z0-9_.-])research/runs(?:/|\b)")
_RELEASE_PLACEHOLDER_MARKERS = (
    "OFFICIAL_" + "REPOSITORY_URL",
)
_PLACEHOLDER_MARKERS = (
    "changeme",
    "dummy",
    "example",
    "fake",
    "hidden",
    "in-memory",
    "local_only",
    "must-never",
    "must-not",
    "not-a-real",
    "placeholder",
    "redacted",
    "replace",
    "sample",
    "passphrase",
    "private password",
    "raw-token",
    "server-returned",
    "supersecret",
    "test-key",
)


@dataclass(frozen=True, order=True, slots=True)
class AuditFinding:
    code: str
    path: str
    detail: str


def _is_environment_example(path: PurePosixPath) -> bool:
    name = path.name.casefold()
    return name == ".env.example" or (name.startswith(".env.") and name.endswith(".example"))


def path_policy_findings(
    relative_path: str | PurePosixPath,
    *,
    is_directory: bool = False,
) -> list[AuditFinding]:
    """Return release-boundary violations encoded by a relative path."""

    path = PurePosixPath(str(relative_path).replace("\\", "/"))
    display = path.as_posix()
    findings: list[AuditFinding] = []
    if path.is_absolute() or ".." in path.parts:
        return [AuditFinding("unsafe_path", display, "Path is absolute or escapes the release root.")]

    lowered_parts = tuple(part.casefold() for part in path.parts)
    directory_parts = lowered_parts if is_directory else lowered_parts[:-1]
    for part in directory_parts:
        if part in _CACHE_DIRECTORY_NAMES or part == "runs" or part.startswith(
            "qdrant_storage"
        ) or part.startswith("qdrant_snapshot"):
            findings.append(
                AuditFinding(
                    "private_path",
                    display,
                    f"Private directory component is not releasable: {part}",
                )
            )
            break
    if directory_parts and directory_parts[0] in _TOP_LEVEL_PRIVATE_DIRECTORIES:
        findings.append(
            AuditFinding(
                "private_path",
                display,
                f"Top-level private directory is not releasable: {directory_parts[0]}",
            )
        )
    if lowered_parts and lowered_parts[0] == "data":
        demo_path = len(lowered_parts) >= 2 and lowered_parts[1] == "demo"
        data_container = is_directory and len(lowered_parts) == 1
        if not demo_path and not data_container:
            findings.append(
                AuditFinding(
                    "private_path",
                    display,
                    "Only the strictly validated data/demo namespace is public.",
                )
            )

    name = path.name.casefold()
    if name in _PRIVATE_FILE_NAMES and not _is_environment_example(path):
        findings.append(AuditFinding("private_file", display, "Private credential or environment file."))
    if name.endswith(_GENERATED_FILE_SUFFIXES):
        findings.append(AuditFinding("generated_artifact", display, "Generated cache/build file."))
    if name.endswith(_PRIVATE_FILE_SUFFIXES) or ".sqlite-" in name:
        findings.append(AuditFinding("private_storage", display, "Database, key, dump, or snapshot file."))
    return findings


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip().strip("\"'").casefold()
    if not normalized or normalized.startswith(("${", "$", "<")):
        return True
    return any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def _credential_has_signal(value: str) -> bool:
    if len(value) < 16 or _looks_like_placeholder(value):
        return False
    classes = sum(
        bool(re.search(pattern, value))
        for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]")
    )
    return classes >= 3 or len(value) >= 28


def _scan_text(path: str, suffix: str, text: str) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for marker in _RELEASE_PLACEHOLDER_MARKERS:
        if marker in text:
            findings.append(
                AuditFinding(
                    "release_placeholder",
                    path,
                    "Unresolved public-release placeholder found.",
                )
            )
            break
    for pattern in _HOST_ABSOLUTE_PATHS:
        if match := pattern.search(text):
            runtime_home = match.group(0).rstrip(".,:;)")
            is_container_runtime_home = (
                PurePosixPath(path).name.casefold().startswith("dockerfile")
                and runtime_home.startswith(
                    ("/" + "home/magicforge", "/" + "home/node")
                )
            )
            if is_container_runtime_home:
                continue
            findings.append(
                AuditFinding(
                    "absolute_local_path",
                    path,
                    f"Host-specific absolute path found near {match.group(0)[:80]!r}.",
                )
            )
            break

    for code, pattern in _TOKEN_PATTERNS:
        match = pattern.search(text)
        if match and not _looks_like_placeholder(match.group(0)):
            findings.append(AuditFinding("secret", path, f"High-confidence credential pattern: {code}."))

    path_parts = {part.casefold() for part in PurePosixPath(path).parts}
    credential_patterns = [] if "tests" in path_parts else [_QUOTED_CREDENTIAL]
    if "tests" not in path_parts and (
        suffix.casefold() in {".conf", ".env", ".ini", ".json", ".toml", ".yaml", ".yml"}
        or Path(path).name.startswith(".env")
    ):
        credential_patterns.append(_BARE_CREDENTIAL)
    for pattern in credential_patterns:
        for match in pattern.finditer(text):
            value = match.group("value")
            if _credential_has_signal(value):
                findings.append(
                    AuditFinding(
                        "secret",
                        path,
                        f"Non-placeholder credential assigned to {match.group('key')}.",
                    )
                )
                break

    posix_path = PurePosixPath(path)
    if suffix.casefold() in _REDISTRIBUTION_EXTENSIONS and _structured_data_denies_redistribution(
        suffix, text
    ):
        findings.append(
            AuditFinding(
                "redistribution_denied",
                path,
                "A source record explicitly denies redistribution.",
            )
        )
    deployment_like = (
        suffix.casefold() in {".env", ".sh", ".toml", ".yaml", ".yml"}
        or posix_path.name.casefold().startswith("dockerfile")
        or posix_path.name.casefold() in {"compose", "magicforge"}
    )
    private_reference_is_guard = (
        posix_path.name == "magicforge"
        and re.search(
            r"(?ms)^\s*case\s+[^\n]+\s+in\s*$.*?research/runs.*?"
            r"die\s+[\"'][^\"']*private host-data boundary[^\"']*[\"'].*?^\s*esac\s*$",
            text,
        )
        is not None
    )
    if (
        deployment_like
        and posix_path.name not in {".dockerignore", ".gitignore"}
        and _PRIVATE_REFERENCE.search(text)
        and not private_reference_is_guard
    ):
        findings.append(
            AuditFinding(
                "private_path_reference",
                path,
                "Deployment/configuration file references the private research run tree.",
            )
        )
    return findings


def _structured_data_denies_redistribution(suffix: str, text: str) -> bool:
    """Find an explicit rights denial in a structured data record.

    Policy prose and source code are intentionally outside this check. JSON is
    parsed so a quoted example string cannot trigger a finding; JSONL is parsed
    one record at a time. YAML/TOML use a line-anchored field matcher because
    these formats are optional dependencies for the release tooling.
    """

    normalized_suffix = suffix.casefold()

    def contains_denial(value: object) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).casefold() == _REDISTRIBUTION_FIELD and item is False:
                    return True
                if contains_denial(item):
                    return True
        elif isinstance(value, list):
            return any(contains_denial(item) for item in value)
        return False

    if normalized_suffix == ".json":
        try:
            return contains_denial(json.loads(text))
        except json.JSONDecodeError:
            return False
    if normalized_suffix == ".jsonl":
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                if contains_denial(json.loads(line)):
                    return True
            except json.JSONDecodeError:
                continue
        return False
    if normalized_suffix in {".toml", ".yaml", ".yml"}:
        return bool(
            re.search(
                rf"(?im)^\s*[\"']?{_REDISTRIBUTION_FIELD}[\"']?\s*[:=]\s*(?:false|no|0)\s*(?:#.*)?$",
                text,
            )
        )
    return False


def _validate_demo_data(root: Path) -> list[AuditFinding]:
    demo_root = root / "data" / "demo"
    if not demo_root.exists():
        return []
    corpus_path = demo_root / "corpus.json"
    display = "data/demo/corpus.json"
    if corpus_path.is_symlink() or not corpus_path.is_file():
        return [
            AuditFinding(
                "demo_metadata_missing",
                display,
                "Public demo data requires a regular corpus.json metadata record.",
            )
        ]
    try:
        payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [AuditFinding("demo_metadata_invalid", display, "Demo corpus metadata is not valid UTF-8 JSON.")]
    if not isinstance(payload, dict):
        return [AuditFinding("demo_metadata_invalid", display, "Demo corpus metadata must be a JSON object.")]

    findings: list[AuditFinding] = []
    for field in ("synthetic", "self_authored", _REDISTRIBUTION_FIELD):
        if payload.get(field) is not True:
            findings.append(
                AuditFinding(
                    "demo_rights_missing",
                    display,
                    f"Public demo metadata must declare {field}=true.",
                )
            )
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        findings.append(AuditFinding("demo_metadata_invalid", display, "Demo corpus requires source declarations."))
    else:
        for index, source in enumerate(sources):
            if not isinstance(source, dict) or source.get("category") != "self_authored_demo":
                findings.append(
                    AuditFinding(
                        "demo_source_not_self_authored",
                        display,
                        f"Demo source {index} is not explicitly self-authored.",
                    )
                )
                continue
            if any(source.get(field) for field in ("doi", "isbn", "provider", "url")):
                findings.append(
                    AuditFinding(
                        "demo_external_source",
                        display,
                        f"Demo source {index} contains an external-source identifier.",
                    )
                )
    return findings


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def audit_tree(root: str | Path, *, max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> list[AuditFinding]:
    """Audit one directory as if every byte beneath it will be public."""

    root_path = Path(root)
    findings: list[AuditFinding] = []
    if max_file_bytes <= 0:
        return [AuditFinding("invalid_policy", ".", "Maximum file size must be positive.")]
    if root_path.is_symlink():
        return [AuditFinding("symlink", ".", "Release root must not be a symbolic link.")]
    if not root_path.exists() or not root_path.is_dir():
        return [AuditFinding("missing_root", ".", "Release root is not a readable directory.")]

    root_path = root_path.resolve()
    try:
        walker = os.walk(root_path, topdown=True, followlinks=False)
        for directory, directory_names, file_names in walker:
            directory_path = Path(directory)
            retained_directories: list[str] = []
            for name in sorted(directory_names):
                child = directory_path / name
                display = _relative(root_path, child)
                if child.is_symlink():
                    findings.append(AuditFinding("symlink", display, "Symbolic links are not releasable."))
                    continue
                directory_findings = path_policy_findings(display, is_directory=True)
                findings.extend(directory_findings)
                if directory_findings:
                    continue
                retained_directories.append(name)
            directory_names[:] = retained_directories

            for name in sorted(file_names):
                path = directory_path / name
                display = _relative(root_path, path)
                if path.is_symlink():
                    findings.append(AuditFinding("symlink", display, "Symbolic links are not releasable."))
                    continue
                findings.extend(path_policy_findings(display))
                try:
                    metadata = path.stat(follow_symlinks=False)
                except OSError:
                    findings.append(AuditFinding("unreadable_file", display, "File metadata could not be read."))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    findings.append(AuditFinding("special_file", display, "Only regular files may be released."))
                    continue
                if metadata.st_size > max_file_bytes:
                    findings.append(
                        AuditFinding(
                            "large_file",
                            display,
                            f"File is {metadata.st_size} bytes; limit is {max_file_bytes}.",
                        )
                    )
                    continue
                try:
                    content = path.read_bytes()
                except OSError:
                    findings.append(AuditFinding("unreadable_file", display, "File content could not be read."))
                    continue
                text = content.decode("utf-8", errors="ignore")
                findings.extend(_scan_text(display, path.suffix, text))
    except OSError:
        findings.append(AuditFinding("unreadable_tree", ".", "Release directory could not be traversed."))
    findings.extend(_validate_demo_data(root_path))
    return sorted(set(findings))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_release_manifest(root: Path) -> list[AuditFinding]:
    """Verify that the deterministic release Manifest covers every payload file."""

    manifest_path = root / _RELEASE_MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return [
            AuditFinding(
                "release_manifest_missing",
                _RELEASE_MANIFEST_NAME,
                "Artifact requires a regular public-release Manifest.",
            )
        ]
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [
            AuditFinding(
                "release_manifest_invalid",
                _RELEASE_MANIFEST_NAME,
                "Public-release Manifest is not valid UTF-8 JSON.",
            )
        ]
    if not isinstance(payload, dict):
        return [
            AuditFinding(
                "release_manifest_invalid",
                _RELEASE_MANIFEST_NAME,
                "Public-release Manifest must be a JSON object.",
            )
        ]

    findings: list[AuditFinding] = []
    if payload.get("schema_version") != _RELEASE_MANIFEST_SCHEMA:
        findings.append(
            AuditFinding(
                "release_manifest_invalid",
                _RELEASE_MANIFEST_NAME,
                "Public-release Manifest schema is unsupported.",
            )
        )
    if payload.get("archive_root") != "magicforge-public" or payload.get("source_date_epoch") != 0:
        findings.append(
            AuditFinding(
                "release_manifest_invalid",
                _RELEASE_MANIFEST_NAME,
                "Archive identity or deterministic epoch is invalid.",
            )
        )

    raw_records = payload.get("files")
    if not isinstance(raw_records, list):
        return sorted(
            set(
                findings
                + [
                    AuditFinding(
                        "release_manifest_invalid",
                        _RELEASE_MANIFEST_NAME,
                        "Manifest files must be an array.",
                    )
                ]
            )
        )

    records: dict[str, dict[str, object]] = {}
    ordered_paths: list[str] = []
    for index, record in enumerate(raw_records):
        if not isinstance(record, dict):
            findings.append(
                AuditFinding(
                    "release_manifest_invalid",
                    _RELEASE_MANIFEST_NAME,
                    f"Manifest record {index} is not an object.",
                )
            )
            continue
        record_path = record.get("path")
        if not isinstance(record_path, str):
            findings.append(
                AuditFinding(
                    "release_manifest_invalid",
                    _RELEASE_MANIFEST_NAME,
                    f"Manifest record {index} has no valid path.",
                )
            )
            continue
        normalized = PurePosixPath(record_path)
        if (
            not record_path
            or "\\" in record_path
            or normalized.is_absolute()
            or ".." in normalized.parts
            or normalized.as_posix() != record_path
            or record_path == _RELEASE_MANIFEST_NAME
            or record_path in records
        ):
            findings.append(
                AuditFinding(
                    "release_manifest_invalid",
                    _RELEASE_MANIFEST_NAME,
                    f"Manifest record {index} has an unsafe or duplicate path.",
                )
            )
            continue
        records[record_path] = record
        ordered_paths.append(record_path)

    if ordered_paths != sorted(ordered_paths):
        findings.append(
            AuditFinding(
                "release_manifest_nondeterministic",
                _RELEASE_MANIFEST_NAME,
                "Manifest records are not sorted by path.",
            )
        )
    if payload.get("file_count") != len(raw_records):
        findings.append(
            AuditFinding(
                "release_manifest_invalid",
                _RELEASE_MANIFEST_NAME,
                "Manifest file_count does not match its record count.",
            )
        )

    actual: dict[str, Path] = {}
    for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        directory_names[:] = sorted(directory_names)
        directory_path = Path(directory)
        for name in sorted(file_names):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            if relative == _RELEASE_MANIFEST_NAME or path.is_symlink() or not path.is_file():
                continue
            actual[relative] = path

    if set(records) != set(actual):
        findings.append(
            AuditFinding(
                "release_manifest_file_set_mismatch",
                _RELEASE_MANIFEST_NAME,
                "Manifest paths do not exactly cover the artifact payload.",
            )
        )
    for relative in sorted(set(records).intersection(actual)):
        record = records[relative]
        path = actual[relative]
        if record.get("size") != path.stat().st_size:
            findings.append(
                AuditFinding(
                    "release_manifest_size_mismatch",
                    relative,
                    "Manifest size does not match the artifact file.",
                )
            )
        checksum = record.get("sha256")
        if not isinstance(checksum, str) or checksum != _file_sha256(path):
            findings.append(
                AuditFinding(
                    "release_manifest_checksum_mismatch",
                    relative,
                    "Manifest SHA-256 does not match the artifact file.",
                )
            )
        declared_mode = record.get("mode")
        actual_mode = f"{stat.S_IMODE(path.stat().st_mode):04o}"
        if declared_mode not in {"0644", "0755"}:
            findings.append(
                AuditFinding(
                    "release_manifest_mode_invalid",
                    relative,
                    "Manifest mode must be normalized to 0644 or 0755.",
                )
            )
        elif declared_mode != actual_mode:
            findings.append(
                AuditFinding(
                    "release_manifest_mode_mismatch",
                    relative,
                    "Manifest mode does not match the artifact file.",
                )
            )
    return sorted(set(findings))


def _tracked_path_findings(root: Path) -> list[AuditFinding]:
    if shutil.which("git") is None:
        return []
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        # A source export or the current local workspace may intentionally have
        # no Git metadata. The allowlist remains the authority in that case.
        return []
    findings: list[AuditFinding] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            relative = raw.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(AuditFinding("tracked_path_invalid", ".", "Git contains a non-UTF-8 path."))
            continue
        policy = path_policy_findings(relative)
        findings.extend(
            AuditFinding("tracked_private_path", item.path, item.detail) for item in policy
        )
        candidate = root / relative
        if candidate.is_symlink():
            findings.append(
                AuditFinding("tracked_symlink", relative, "A Git-tracked symbolic link is not releasable.")
            )
    return findings


def audit_source_tree(
    root: str | Path,
    *,
    allowlist: str | Path = "release/public-allowlist.txt",
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> list[AuditFinding]:
    """Audit only the positive public selection plus Git-tracked path policy.

    Private, untracked corpus trees may remain beside the source checkout and
    are deliberately not traversed in this mode.
    """

    original_root = Path(root)
    if original_root.is_symlink() or not original_root.is_dir():
        return [AuditFinding("missing_root", ".", "Source root is not a regular directory.")]
    source_root = original_root.resolve()
    allowlist_path = Path(allowlist)
    if not allowlist_path.is_absolute():
        allowlist_path = source_root / allowlist_path
    try:
        if __package__:
            from .build_public_release import collect_allowlisted_files, load_allowlist
        else:  # pragma: no cover - direct CLI path
            from build_public_release import collect_allowlisted_files, load_allowlist

        selected = collect_allowlisted_files(source_root, load_allowlist(allowlist_path))
    except Exception as exc:
        return [AuditFinding("allowlist_invalid", "release/public-allowlist.txt", str(exc))]

    findings = _tracked_path_findings(source_root)
    with tempfile.TemporaryDirectory(prefix="magicforge-source-audit-") as temporary:
        stage = Path(temporary) / "magicforge-public"
        stage.mkdir()
        try:
            for source in selected:
                destination = stage / source.relative_to(source_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination, follow_symlinks=False)
        except OSError:
            findings.append(
                AuditFinding("source_copy_failed", ".", "Allowlisted source could not be isolated for audit.")
            )
        else:
            findings.extend(audit_tree(stage, max_file_bytes=max_file_bytes))
    return sorted(set(findings))


def audit_artifact(
    artifact: str | Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> list[AuditFinding]:
    """Audit an extracted directory or deterministic public ZIP artifact."""

    path = Path(artifact)
    if path.is_dir():
        resolved = path.resolve()
        return sorted(
            set(
                audit_tree(resolved, max_file_bytes=max_file_bytes)
                + _validate_release_manifest(resolved)
            )
        )
    if path.is_symlink() or not path.is_file() or path.suffix.casefold() != ".zip":
        return [AuditFinding("artifact_invalid", ".", "Artifact must be a directory or ZIP file.")]

    findings: list[AuditFinding] = []
    with tempfile.TemporaryDirectory(prefix="magicforge-artifact-audit-") as temporary:
        temporary_root = Path(temporary)
        archive_modes: dict[str, int] = {}
        try:
            with zipfile.ZipFile(path) as archive:
                seen: set[str] = set()
                for info in archive.infolist():
                    name = info.filename
                    member = PurePosixPath(name)
                    if (
                        not name
                        or "\\" in name
                        or member.is_absolute()
                        or ".." in member.parts
                        or name in seen
                    ):
                        findings.append(AuditFinding("archive_path_invalid", name or ".", "Unsafe ZIP member."))
                        continue
                    seen.add(name)
                    mode = info.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        findings.append(AuditFinding("symlink", name, "ZIP contains a symbolic link."))
                    if name.endswith("/") or not stat.S_ISREG(mode):
                        findings.append(
                            AuditFinding(
                                "archive_mode_invalid",
                                name,
                                "ZIP payload entries must be regular POSIX files.",
                            )
                        )
                    else:
                        permissions = stat.S_IMODE(mode)
                        if info.create_system != 3 or permissions not in {0o644, 0o755}:
                            findings.append(
                                AuditFinding(
                                    "archive_mode_invalid",
                                    name,
                                    "ZIP file mode must be normalized POSIX 0644 or 0755.",
                                )
                            )
                        else:
                            archive_modes[name] = permissions
                    if info.file_size > max_file_bytes:
                        findings.append(
                            AuditFinding(
                                "large_file",
                                name,
                                f"ZIP member is {info.file_size} bytes; limit is {max_file_bytes}.",
                            )
                        )
                if findings:
                    return sorted(set(findings))
                archive.extractall(temporary_root)
                for name, permissions in archive_modes.items():
                    os.chmod(temporary_root.joinpath(*PurePosixPath(name).parts), permissions)
        except (OSError, zipfile.BadZipFile):
            return [AuditFinding("artifact_invalid", ".", "ZIP artifact is unreadable or malformed.")]

        children = list(temporary_root.iterdir())
        if len(children) != 1 or children[0].name != "magicforge-public" or not children[0].is_dir():
            findings.append(
                AuditFinding(
                    "archive_root_invalid",
                    ".",
                    "ZIP must contain exactly one magicforge-public root directory.",
                )
            )
        else:
            findings.extend(audit_tree(children[0], max_file_bytes=max_file_bytes))
            findings.extend(_validate_release_manifest(children[0]))
    return sorted(set(findings))


def format_report(findings: Sequence[AuditFinding], *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(
            {"ok": not findings, "finding_count": len(findings), "findings": [asdict(item) for item in findings]},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    if not findings:
        return "Public release audit passed."
    lines = [f"Public release audit failed with {len(findings)} finding(s):"]
    lines.extend(f"- [{item.code}] {item.path}: {item.detail}" for item in findings)
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", help="Backward-compatible artifact directory.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--source", type=Path, help="Audit the allowlisted selection in a source checkout.")
    mode.add_argument("--artifact", type=Path, help="Audit an extracted directory or public ZIP.")
    parser.add_argument("--allowlist", type=Path, default=Path("release/public-allowlist.txt"))
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    parser.add_argument("--json", action="store_true", help="Emit a deterministic JSON report.")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.source is not None and args.root is not None:
        parser.error("positional root cannot be combined with --source")
    if args.artifact is not None and args.root is not None:
        parser.error("positional root cannot be combined with --artifact")
    if args.source is not None:
        findings = audit_source_tree(
            args.source,
            allowlist=args.allowlist,
            max_file_bytes=args.max_file_bytes,
        )
    else:
        artifact = args.artifact or args.root
        if artifact is None:
            parser.error("provide --source, --artifact, or an artifact directory")
        findings = audit_artifact(artifact, max_file_bytes=args.max_file_bytes)
    print(format_report(findings, as_json=args.json))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
