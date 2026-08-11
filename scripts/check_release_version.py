#!/usr/bin/env python3
"""Fail closed unless every public version surface matches one release tag."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionError(RuntimeError):
    pass


def _one(pattern: str, text: str, label: str) -> str:
    matches = re.findall(pattern, text, flags=re.MULTILINE)
    if len(matches) != 1:
        raise ReleaseVersionError(f"{label} does not expose exactly one version")
    return str(matches[0])


def inspect_versions() -> dict[str, str]:
    package = json.loads(
        (ROOT / "frontend/package.json").read_text(encoding="utf-8")
    )
    package_lock = json.loads(
        (ROOT / "frontend/package-lock.json").read_text(encoding="utf-8")
    )
    app_version = _one(
        r'^\s*version="([^"]+)",$',
        (ROOT / "app/main.py").read_text(encoding="utf-8"),
        "FastAPI",
    )
    backup_version = _one(
        r'^APPLICATION_VERSION = "([^"]+)"$',
        (ROOT / "scripts/ops/backup_restore.py").read_text(encoding="utf-8"),
        "backup metadata",
    )
    compose_versions = set(
        re.findall(
            r"^\s*image:\s+magicforge-[^:\s]+:([^\s]+)\s*$",
            (ROOT / "compose.yaml").read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
    )
    if len(compose_versions) != 1:
        raise ReleaseVersionError("Compose images do not share one public version")
    versions = {
        "api": app_version,
        "backup": backup_version,
        "compose": compose_versions.pop(),
        "frontend": str(package.get("version") or ""),
        "frontend_lock": str(package_lock.get("version") or ""),
        "frontend_lock_root": str(
            (package_lock.get("packages") or {}).get("", {}).get("version") or ""
        ),
    }
    unique = set(versions.values())
    if len(unique) != 1 or not next(iter(unique), ""):
        detail = ", ".join(f"{key}={value!r}" for key, value in versions.items())
        raise ReleaseVersionError(f"public version metadata disagrees: {detail}")
    version = unique.pop()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{version}]" not in changelog:
        raise ReleaseVersionError("CHANGELOG has no entry for the public version")
    return versions


def validate_tag(tag: str) -> str:
    versions = inspect_versions()
    version = versions["api"]
    if tag != f"v{version}":
        raise ReleaseVersionError(
            f"release ref {tag!r} does not match code version v{version}"
        )
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    arguments = parser.parse_args()
    try:
        version = validate_tag(arguments.tag)
    except ReleaseVersionError as exc:
        print(f"release version check failed: {exc}")
        return 1
    print(f"release version check passed for v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
