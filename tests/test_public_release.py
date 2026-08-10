from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from pathlib import Path

import pytest

from scripts.audit_public_release import audit_artifact, audit_source_tree, audit_tree
from scripts.build_public_release import ReleaseBuildError, build_release, load_allowlist


def _write(path: Path, content: str | bytes = "safe\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def test_audit_accepts_small_public_source_tree(tmp_path: Path) -> None:
    root = tmp_path / "public"
    _write(root / "README.md", "# Public source\n")
    _write(root / ".env.example", "GLM_API_KEY=\n")
    _write(root / "package" / "module.py", "VALUE = 1\n")

    assert audit_tree(root) == []


def test_audit_rejects_every_private_boundary(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    _write(root / "research" / "runs" / "run-001" / "source.json", "{}\n")
    _write(root / "output" / "capture.txt")
    _write(root / "storage" / "governance.sqlite", b"sqlite")
    _write(root / "nested" / "governance.sqlite", b"sqlite")
    _write(root / "snapshots" / "corpus.snapshot", b"snapshot")
    _write(root / ".magicforge" / "runtime.json", "{}\n")
    _write(root / "host-path.txt", "/" + "home/developer/private/source.txt\n")
    token = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    _write(root / "credential.py", f'token = "{token}"\n')
    field = "redistribution_" + "allowed"
    _write(root / "rights.json", json.dumps({field: False}) + "\n")
    _write(root / "too-large.bin", b"x" * 257)
    target = _write(root / "safe-target.txt")
    try:
        os.symlink(target, root / "linked.txt")
    except (NotImplementedError, OSError):
        pass

    findings = audit_tree(root, max_file_bytes=256)
    codes = {finding.code for finding in findings}
    assert {
        "absolute_local_path",
        "large_file",
        "private_path",
        "private_storage",
        "redistribution_denied",
        "secret",
    }.issubset(codes)
    if (root / "linked.txt").is_symlink():
        assert "symlink" in codes
    assert all(not Path(finding.path).is_absolute() for finding in findings)


def test_redistribution_check_reads_data_records_not_policy_prose(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    field = "redistribution_" + "allowed"
    _write(root / "rights.json", json.dumps({"nested": [{field: False}]}) + "\n")
    _write(
        root / "DATA_POLICY.md",
        f"Reject a record when `{field}=false`; this sentence is policy, not data.\n",
    )

    findings = audit_tree(root)
    denied = [finding for finding in findings if finding.code == "redistribution_denied"]
    assert [finding.path for finding in denied] == ["rights.json"]


def test_container_runtime_home_and_private_boundary_guard_are_public_safe(
    tmp_path: Path,
) -> None:
    root = tmp_path / "candidate"
    _write(root / "Dockerfile", "ENV HOME=/" + "home/magicforge\n")
    _write(
        root / "magicforge",
        """case "${source}" in
  "${ROOT_DIR}"/research/runs/*)
    die "container crosses the private host-data boundary"
    ;;
esac
""",
    )

    assert audit_tree(root) == []


def test_allowlist_syntax_is_narrow_and_fail_closed(tmp_path: Path) -> None:
    private = _write(tmp_path / "private.txt", "research/runs/**\n")
    wildcard = _write(tmp_path / "wildcard.txt", "src/*.py\n")

    with pytest.raises(ReleaseBuildError, match="private path"):
        load_allowlist(private)
    with pytest.raises(ReleaseBuildError, match="wildcard"):
        load_allowlist(wildcard)


def test_positive_build_excludes_unlisted_private_data_and_is_reproducible(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write(source / "README.md", "# MagicForge\n")
    _write(source / "package" / "module.py", "ANSWER = 42\n")
    _write(source / "package" / "__pycache__" / "module.pyc", b"generated")
    _write(source / "research" / "runs" / "private" / "exact-content.json", "{}\n")
    allowlist = _write(
        source / "public-allowlist.txt",
        "README.md\npackage/**\npublic-allowlist.txt\n",
    )

    first = build_release(source, allowlist, tmp_path / "first.zip")
    second = build_release(source, allowlist, tmp_path / "second.zip")

    assert first.sha256 == second.sha256
    assert first.artifact.read_bytes() == second.artifact.read_bytes()
    assert hashlib.sha256(first.artifact.read_bytes()).hexdigest() == first.sha256
    assert first.checksum_file.read_text(encoding="ascii").startswith(first.sha256)

    with zipfile.ZipFile(first.artifact) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert "magicforge-public/README.md" in names
        assert "magicforge-public/package/module.py" in names
        assert "magicforge-public/PUBLIC_RELEASE_MANIFEST.json" in names
        assert not any("runs" in name or "__pycache__" in name for name in names)
        manifest = json.loads(
            archive.read("magicforge-public/PUBLIC_RELEASE_MANIFEST.json")
        )
        assert manifest["source_date_epoch"] == 0
        assert manifest["file_count"] == 3
        assert {record["mode"] for record in manifest["files"]} == {"0644"}
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
    assert audit_artifact(first.artifact) == []


def test_artifact_audit_verifies_manifest_completeness_and_checksums(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write(source / "README.md", "# MagicForge\n")
    allowlist = _write(source / "allowlist.txt", "README.md\nallowlist.txt\n")
    result = build_release(source, allowlist, tmp_path / "release.zip")
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(result.artifact) as archive:
        archive.extractall(extracted)
    artifact_root = extracted / "magicforge-public"

    _write(artifact_root / "unexpected.txt", "not declared\n")
    assert "release_manifest_file_set_mismatch" in {
        finding.code for finding in audit_artifact(artifact_root)
    }
    (artifact_root / "unexpected.txt").unlink()
    _write(artifact_root / "README.md", "tampered\n")
    codes = {finding.code for finding in audit_artifact(artifact_root)}
    assert "release_manifest_checksum_mismatch" in codes
    assert "release_manifest_size_mismatch" in codes


def test_archive_preserves_normalized_executable_mode(tmp_path: Path) -> None:
    source = tmp_path / "source"
    launcher = _write(source / "magicforge", "#!/bin/sh\nexit 0\n")
    launcher.chmod(0o777)
    allowlist = _write(source / "allowlist.txt", "magicforge\nallowlist.txt\n")

    result = build_release(source, allowlist, tmp_path / "release.zip")
    with zipfile.ZipFile(result.artifact) as archive:
        info = archive.getinfo("magicforge-public/magicforge")
        assert info.create_system == 3
        assert stat.S_ISREG(info.external_attr >> 16)
        assert stat.S_IMODE(info.external_attr >> 16) == 0o755
        manifest = json.loads(
            archive.read("magicforge-public/PUBLIC_RELEASE_MANIFEST.json")
        )
        record = next(item for item in manifest["files"] if item["path"] == "magicforge")
        assert record["mode"] == "0755"

    assert audit_artifact(result.artifact) == []


def test_source_audit_ignores_untracked_private_runs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "README.md", "# Public\n")
    _write(source / "release" / "public-allowlist.txt", "README.md\nrelease/public-allowlist.txt\n")
    _write(source / "research" / "runs" / "private" / "source.json", "{}\n")

    assert audit_source_tree(source) == []


def test_demo_data_requires_explicit_public_rights(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    _write(
        root / "data" / "demo" / "corpus.json",
        json.dumps(
            {
                "synthetic": True,
                "self_authored": True,
                "redistribution_allowed": False,
                "sources": [{"category": "self_authored_demo"}],
            }
        ),
    )

    codes = {finding.code for finding in audit_tree(root)}
    assert "demo_rights_missing" in codes
    assert "redistribution_denied" in codes


def test_build_rejects_secret_in_an_allowlisted_file(tmp_path: Path) -> None:
    source = tmp_path / "source"
    token = "tv" + "ly-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    _write(source / "config.py", f'api_key = "{token}"\n')
    allowlist = _write(source / "allowlist.txt", "config.py\nallowlist.txt\n")

    with pytest.raises(ReleaseBuildError, match="secret"):
        build_release(source, allowlist, tmp_path / "release.zip")


def test_build_rejects_selected_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = _write(source / "target.txt")
    link = source / "link.txt"
    try:
        os.symlink(target, link)
    except (NotImplementedError, OSError):
        pytest.skip("symbolic links are unavailable on this platform")
    allowlist = _write(source / "allowlist.txt", "link.txt\nallowlist.txt\n")

    with pytest.raises(ReleaseBuildError, match="symbolic link"):
        build_release(source, allowlist, tmp_path / "release.zip")


def test_build_requires_zip_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "README.md")
    allowlist = _write(source / "allowlist.txt", "README.md\nallowlist.txt\n")

    with pytest.raises(ReleaseBuildError, match=r"\.zip"):
        build_release(source, allowlist, tmp_path / "release-directory")


def test_root_private_ignore_rules_do_not_hide_nested_source_directories() -> None:
    root = Path(__file__).resolve().parents[1]
    gitignore = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "/storage/" in gitignore
    assert "storage/" not in gitignore
    assert "/output/" in gitignore
    assert "/data/*" in gitignore
    assert "!/data/demo/**" in gitignore
    assert "/storage" in dockerignore
    assert "storage" not in dockerignore
    assert "/output" in dockerignore
    assert "/data/*" in dockerignore
    assert "!/data/demo/**" in dockerignore


def test_all_runtime_profiles_have_public_safe_environment_examples() -> None:
    root = Path(__file__).resolve().parents[1]
    allowlist = (root / "release" / "public-allowlist.txt").read_text(encoding="utf-8")
    for profile in ("demo", "development", "production"):
        relative = f".env.{profile}.example"
        content = (root / relative).read_text(encoding="utf-8")
        assert f"MAGICFORGE_PROFILE={profile}" in content
        assert relative in allowlist.splitlines()
