#!/usr/bin/env python3
"""Build a deterministic, allowlist-only MagicForge public source archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

if __package__:
    from .audit_public_release import (
        DEFAULT_MAX_FILE_BYTES,
        AuditFinding,
        audit_tree,
        format_report,
        path_policy_findings,
    )
else:  # pragma: no cover - exercised by the CLI subprocess path
    from audit_public_release import (  # type: ignore[no-redef]
        DEFAULT_MAX_FILE_BYTES,
        AuditFinding,
        audit_tree,
        format_report,
        path_policy_findings,
    )


DEFAULT_ALLOWLIST = Path("release/public-allowlist.txt")
DEFAULT_OUTPUT = Path("dist/magicforge-public.zip")
ARCHIVE_ROOT = "magicforge-public"
MANIFEST_NAME = "PUBLIC_RELEASE_MANIFEST.json"
_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class ReleaseBuildError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AllowRule:
    path: PurePosixPath
    subtree: bool


@dataclass(frozen=True, slots=True)
class BuildResult:
    artifact: Path
    checksum_file: Path
    sha256: str
    file_count: int


def _normalize_rule(raw: str, line_number: int) -> AllowRule:
    value = raw.strip()
    subtree = value.endswith("/**")
    base = value[:-3] if subtree else value
    if not base or base.startswith(("/", "\\")) or "\\" in base:
        raise ReleaseBuildError(f"Invalid allowlist path on line {line_number}.")
    if any(character in base for character in "*?[]"):
        raise ReleaseBuildError(f"Unsupported allowlist wildcard on line {line_number}.")
    path = PurePosixPath(base)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ReleaseBuildError(f"Unsafe allowlist path on line {line_number}.")
    policy = path_policy_findings(path, is_directory=subtree)
    if policy:
        raise ReleaseBuildError(
            f"Allowlist line {line_number} selects a private path: {policy[0].code}."
        )
    return AllowRule(path=path, subtree=subtree)


def load_allowlist(path: str | Path) -> tuple[AllowRule, ...]:
    allowlist_path = Path(path)
    try:
        lines = allowlist_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseBuildError("Public release allowlist could not be read.") from exc
    rules = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rules.append(_normalize_rule(stripped, line_number))
    if not rules:
        raise ReleaseBuildError("Public release allowlist is empty.")
    if len({(rule.path, rule.subtree) for rule in rules}) != len(rules):
        raise ReleaseBuildError("Public release allowlist contains duplicate rules.")
    return tuple(rules)


def _assert_inside_root(root: Path, candidate: Path) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ReleaseBuildError("Allowlisted path escapes the source root.") from exc


def _assert_no_symlink(root: Path, candidate: Path) -> None:
    current = root
    for part in candidate.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ReleaseBuildError(f"Allowlisted symbolic link is forbidden: {candidate.relative_to(root)}")


def _is_generated_cache(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    parts = {part.casefold() for part in relative.parts}
    return bool(
        parts.intersection({"__pycache__", ".next", ".pytest_cache", "node_modules"})
        or relative.name.casefold().endswith((".pyc", ".pyo", ".tsbuildinfo"))
    )


def collect_allowlisted_files(source_root: str | Path, rules: Sequence[AllowRule]) -> tuple[Path, ...]:
    original_root = Path(source_root)
    if original_root.is_symlink():
        raise ReleaseBuildError("Source root must not be a symbolic link.")
    root = original_root.resolve()
    if not root.is_dir():
        raise ReleaseBuildError("Source root is not a directory.")
    selected: dict[str, Path] = {}

    for rule in rules:
        candidate = root.joinpath(*rule.path.parts)
        _assert_inside_root(root, candidate)
        _assert_no_symlink(root, candidate)
        if rule.subtree:
            if not candidate.is_dir():
                raise ReleaseBuildError(f"Allowlisted directory is missing: {rule.path.as_posix()}")
            matches = sorted(candidate.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
            matched_files = 0
            for path in matches:
                if _is_generated_cache(path, root):
                    continue
                _assert_no_symlink(root, path)
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise ReleaseBuildError(f"Allowlisted path is not a regular file: {path.relative_to(root)}")
                relative = path.relative_to(root).as_posix()
                policy = path_policy_findings(relative)
                if policy:
                    raise ReleaseBuildError(f"Allowlisted private path rejected: {relative} ({policy[0].code})")
                selected[relative] = path
                matched_files += 1
            if not matched_files:
                raise ReleaseBuildError(f"Allowlisted directory is empty: {rule.path.as_posix()}")
        else:
            if not candidate.is_file():
                raise ReleaseBuildError(f"Allowlisted file is missing: {rule.path.as_posix()}")
            relative = candidate.relative_to(root).as_posix()
            selected[relative] = candidate

    if not selected:
        raise ReleaseBuildError("Allowlist selected no files.")
    return tuple(selected[name] for name in sorted(selected))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(stage: Path, files: Sequence[Path]) -> None:
    records = []
    for path in sorted(files, key=lambda item: item.relative_to(stage).as_posix()):
        records.append(
            {
                "path": path.relative_to(stage).as_posix(),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
                "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
            }
        )
    manifest = {
        "archive_root": ARCHIVE_ROOT,
        "file_count": len(records),
        "files": records,
        "schema_version": "magicforge-public-release-manifest-1",
        "source_date_epoch": 0,
    }
    (stage / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_deterministic_zip(stage: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, mode="w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as archive:
        for path in sorted(stage.rglob("*"), key=lambda item: item.relative_to(stage).as_posix()):
            if not path.is_file():
                continue
            relative = path.relative_to(stage).as_posix()
            info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{relative}", date_time=_FIXED_ZIP_TIME)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED
            normalized_mode = stat.S_IMODE(path.stat().st_mode)
            info.external_attr = ((stat.S_IFREG | normalized_mode) & 0xFFFF) << 16
            info.flag_bits = 0
            archive.writestr(info, path.read_bytes())


def _atomic_replace(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ReleaseBuildError(f"Refusing to replace symbolic link: {destination.name}")
    os.replace(source, destination)


def build_release(
    source_root: str | Path,
    allowlist: str | Path,
    output: str | Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> BuildResult:
    original_root = Path(source_root)
    if original_root.is_symlink():
        raise ReleaseBuildError("Source root must not be a symbolic link.")
    root = original_root.resolve()
    original_output = Path(output)
    if original_output.suffix.casefold() != ".zip":
        raise ReleaseBuildError("--output must name a .zip file.")
    if original_output.is_symlink():
        raise ReleaseBuildError("Refusing to replace symbolic-link artifact.")
    output_path = original_output.resolve()
    allowlist_path = Path(allowlist)
    if not allowlist_path.is_absolute():
        allowlist_path = root / allowlist_path
    rules = load_allowlist(allowlist_path)
    source_files = collect_allowlisted_files(root, rules)

    with tempfile.TemporaryDirectory(prefix="magicforge-public-") as temporary:
        temporary_root = Path(temporary)
        stage = temporary_root / ARCHIVE_ROOT
        stage.mkdir()
        copied: list[Path] = []
        for source in source_files:
            relative = source.relative_to(root)
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination, follow_symlinks=False)
            normalized_mode = 0o755 if stat.S_IMODE(source.stat().st_mode) & 0o111 else 0o644
            os.chmod(destination, normalized_mode)
            copied.append(destination)
        _write_manifest(stage, copied)

        findings = audit_tree(stage, max_file_bytes=max_file_bytes)
        if findings:
            raise ReleaseBuildError(format_report(findings))

        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.is_symlink():
            raise ReleaseBuildError("Refusing to replace symbolic-link artifact.")
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent, delete=False
        ) as handle:
            temporary_artifact = Path(handle.name)
        try:
            _write_deterministic_zip(stage, temporary_artifact)
            digest = _sha256(temporary_artifact)
            _atomic_replace(temporary_artifact, output_path)
        finally:
            temporary_artifact.unlink(missing_ok=True)

    checksum_path = output_path.with_name(output_path.name + ".sha256")
    if checksum_path.is_symlink():
        raise ReleaseBuildError("Refusing to replace symbolic-link checksum file.")
    checksum_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="ascii",
        newline="\n",
        prefix=f".{checksum_path.name}.",
        suffix=".tmp",
        dir=checksum_path.parent,
        delete=False,
    ) as handle:
        handle.write(f"{digest}  {output_path.name}\n")
        checksum_temp = Path(handle.name)
    try:
        _atomic_replace(checksum_temp, checksum_path)
    finally:
        checksum_temp.unlink(missing_ok=True)
    return BuildResult(
        artifact=output_path,
        checksum_file=checksum_path,
        sha256=digest,
        file_count=len(source_files),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = build_release(
            args.source_root,
            args.allowlist,
            args.output,
            max_file_bytes=args.max_file_bytes,
        )
    except ReleaseBuildError as exc:
        print(f"Public release build failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "artifact": str(result.artifact),
                "checksum_file": str(result.checksum_file),
                "file_count": result.file_count,
                "sha256": result.sha256,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
