from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from scripts import check_release_version
from scripts.ops import backup_restore, compose_static_check, doctor, full_stack_smoke


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "magicforge"
COMPOSE = ROOT / "compose.yaml"
DEMO_CORPUS = ROOT / "data/demo/corpus.json"
PUBLIC_VERSION = "0.1.0-alpha.3"


def test_public_release_version_metadata_agrees() -> None:
    frontend_package = json.loads(
        (ROOT / "frontend/package.json").read_text(encoding="utf-8")
    )
    frontend_lock = json.loads(
        (ROOT / "frontend/package-lock.json").read_text(encoding="utf-8")
    )
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    magicforge_images = {
        str(service["image"])
        for service in compose["services"].values()
        if str(service.get("image", "")).startswith("magicforge-")
    }

    assert frontend_package["version"] == PUBLIC_VERSION
    assert frontend_lock["version"] == PUBLIC_VERSION
    assert frontend_lock["packages"][""]["version"] == PUBLIC_VERSION
    assert magicforge_images
    assert all(image.endswith(f":{PUBLIC_VERSION}") for image in magicforge_images)
    assert (
        f'version="{PUBLIC_VERSION}"'
        in (ROOT / "app/main.py").read_text(encoding="utf-8")
    )
    assert (
        f"## [{PUBLIC_VERSION}]"
        in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    )
    assert backup_restore.APPLICATION_VERSION == PUBLIC_VERSION
    assert check_release_version.validate_tag(f"v{PUBLIC_VERSION}") == PUBLIC_VERSION
    with pytest.raises(check_release_version.ReleaseVersionError, match="does not match"):
        check_release_version.validate_tag("v0.1.0-alpha.2")


def test_compose_profile_variables_are_declared_in_environment_examples() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    for profile in ("development", "production"):
        prefix = f"MAGICFORGE_{profile.upper()}_"
        required = set(re.findall(rf"\$\{{({prefix}[A-Z0-9_]+)", compose))
        example = (ROOT / f".env.{profile}.example").read_text(encoding="utf-8")
        declared = {
            line.split("=", 1)[0]
            for line in example.splitlines()
            if line and not line.startswith("#") and "=" in line
        }
        assert required <= declared


def _run_cli(
    *arguments: str,
    docker_bin: str | None = None,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    if docker_bin is not None:
        environment["MAGICFORGE_DOCKER_BIN"] = docker_bin
    if extra_environment is not None:
        environment.update(extra_environment)
    return subprocess.run(
        ["bash", str(CLI), *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_fake_docker(tmp_path: Path) -> Path:
    docker = tmp_path / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -eu

state_dir="${FAKE_DOCKER_STATE_DIR:?}"
printf '%s\n' "$*" >> "${state_dir}/calls.log"

case "${1:-}" in
  info)
    exit 0
    ;;
  inspect)
    container="${@: -1}"
    case "${container}" in
      postgres-container|qdrant-container|api-container)
        printf 'running|healthy|0\n'
        ;;
      web-container)
        count_file="${state_dir}/web-inspections"
        count=0
        if [[ -f "${count_file}" ]]; then
          count="$(cat "${count_file}")"
        fi
        count=$((count + 1))
        printf '%s\n' "${count}" > "${count_file}"
        if ((count == 1)); then
          printf 'running|starting|0\n'
        else
          printf 'running|healthy|0\n'
        fi
        ;;
      migrate-container)
        printf 'exited|missing|0\n'
        ;;
      seed-container)
        printf 'exited|missing|%s\n' "${FAKE_DOCKER_SEED_EXIT:-0}"
        ;;
      *)
        exit 2
        ;;
    esac
    exit 0
    ;;
  compose)
    if [[ " $* " == *" version "* ]]; then
      exit 0
    fi
    if [[ " $* " == *" ps --all --quiet "* ]]; then
      service="${@: -1}"
      printf '%s-container\n' "${service}"
    fi
    exit 0
    ;;
esac

exit 2
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return docker


def _write_fake_runtime_doctor(tmp_path: Path) -> tuple[Path, Path]:
    docker = tmp_path / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -eu

state_dir="${FAKE_DOCKER_STATE_DIR:?}"
printf '%s\n' "$*" >> "${state_dir}/doctor-calls.log"

case "${1:-}" in
  info)
    exit 0
    ;;
  inspect)
    format="${3:-}"
    if [[ "${format}" == *'.State.Status'* ]]; then
      printf 'exited 0\n'
    elif [[ "${format}" == *'.State.Health'* ]]; then
      printf 'healthy\n'
    fi
    exit 0
    ;;
  compose)
    if [[ " $* " == *" version "* || " $* " == *" config --quiet "* ]]; then
      exit 0
    fi
    if [[ " $* " == *" ps --services --filter status=running "* ]]; then
      printf '%s\n' postgres qdrant api-dev web-dev
      exit 0
    fi
    if [[ " $* " == *" ps --all --quiet migrate "* ]]; then
      printf 'migrate-container\n'
      exit 0
    fi
    if [[ " $* " == *" ps --quiet "* ]]; then
      service="${@: -1}"
      printf '%s-container\n' "${service}"
      exit 0
    fi
    if [[ " $* " == *" exec --no-TTY --env PYTHONDONTWRITEBYTECODE=1 api-dev python -m scripts.ops.doctor "* ]]; then
      printf '{"status":"ready","profile":"development"}\n'
      exit 0
    fi
    exit 0
    ;;
esac

exit 2
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    curl = tmp_path / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "${FAKE_DOCKER_STATE_DIR:?}/curl-calls.log"
exit 0
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    return docker, curl


def _write_clean_host_docker(tmp_path: Path) -> tuple[Path, Path]:
    model = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    model_path = tmp_path / "compose-model.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")
    docker = tmp_path / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -eu

state_dir="${FAKE_DOCKER_STATE_DIR:?}"
printf '%s\n' "$*" >> "${state_dir}/clean-host-calls.log"

case "${1:-}" in
  info)
    exit 0
    ;;
  inspect)
    format="${3:-}"
    if [[ "${format}" == *'.Mounts'* ]]; then
      exit 0
    fi
    container="${@: -1}"
    if [[ "${format}" == *'}}|{{if .State.Health'* ]]; then
      case "${container}" in
        migrate-container|seed-container) printf 'exited|missing|0\n' ;;
        *) printf 'running|healthy|0\n' ;;
      esac
    elif [[ "${format}" == *'.State.Status}} {{.State.ExitCode'* ]]; then
      printf 'exited 0\n'
    elif [[ "${format}" == *'.State.Health'* ]]; then
      printf 'healthy\n'
    fi
    exit 0
    ;;
  run)
    cat >/dev/null
    [[ " $* " == *" --network none "* ]]
    [[ " $* " == *" --read-only "* ]]
    [[ " $* " == *" --compose-json-stdin "* ]]
    printf 'static doctor passed for demo\n'
    exit 0
    ;;
  compose)
    if [[ " $* " == *" version "* || " $* " == *" config --quiet "* ]]; then
      exit 0
    fi
    if [[ " $* " == *" config --format json --no-interpolate "* ]]; then
      cat "${FAKE_COMPOSE_MODEL:?}"
      exit 0
    fi
    if [[ " $* " == *" ps --services --filter status=running "* ]]; then
      printf '%s\n' postgres qdrant api web
      exit 0
    fi
    if [[ " $* " == *" ps --all --quiet migrate "* ]]; then
      printf 'migrate-container\n'
      exit 0
    fi
    if [[ " $* " == *" ps --all --quiet seed "* ]]; then
      printf 'seed-container\n'
      exit 0
    fi
    if [[ " $* " == *" ps --all --quiet "* ]]; then
      service="${@: -1}"
      if [[ "${service}" == "--quiet" ]]; then
        printf '%s\n' postgres-container qdrant-container api-container web-container
      else
        printf '%s-container\n' "${service}"
      fi
      exit 0
    fi
    if [[ " $* " == *" ps --quiet "* ]]; then
      service="${@: -1}"
      printf '%s-container\n' "${service}"
      exit 0
    fi
    if [[ " $* " == *" exec --no-TTY --env PYTHONDONTWRITEBYTECODE=1 api python -m scripts.ops.doctor "* ]]; then
      printf '{"status":"ready","profile":"demo"}\n'
      exit 0
    fi
    if [[ " $* " == *" exec --no-TTY --env PYTHONDONTWRITEBYTECODE=1 api python -m scripts.ops.full_stack_smoke --web-url http://web:3000 "* ]]; then
      printf '{"status":"passed","pages":4}\n'
      exit 0
    fi
    exit 0
    ;;
esac

exit 2
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    curl = tmp_path / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "${FAKE_DOCKER_STATE_DIR:?}/clean-host-http.log"
exit 0
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    return docker, model_path


@pytest.mark.parametrize("profile", ["demo", "development", "production"])
def test_static_delivery_check_passes(profile: str) -> None:
    compose_static_check.validate(profile)


def test_static_delivery_uses_compose_json_without_pyyaml(monkeypatch) -> None:
    model = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    commands: list[list[str]] = []

    def no_pyyaml() -> object:
        raise ModuleNotFoundError("No module named 'yaml'")

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(model), "")

    monkeypatch.setattr(compose_static_check, "_load_with_pyyaml", no_pyyaml)
    monkeypatch.setattr(compose_static_check.subprocess, "run", fake_run)

    compose_static_check.validate("demo")

    assert len(commands) == 1
    command = commands[0]
    assert command[:2] == ["docker", "compose"]
    assert "--env-file" in command
    assert command[command.index("--profile") + 1] == "*"
    assert "--format" in command
    assert command[command.index("--format") + 1] == "json"
    assert "--no-interpolate" in command


def test_static_delivery_accepts_pre_rendered_compose_without_weakening_checks() -> None:
    model = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    model["services"]["api-production"]["environment"][
        "PRODUCTION_QDRANT_WRITES_ENABLED"
    ] = "true"

    with pytest.raises(
        compose_static_check.ComposeCheckError,
        match="enables persistent writes",
    ):
        compose_static_check.validate("demo", model=model)


def test_static_delivery_compose_fallback_keeps_production_fail_closed(monkeypatch) -> None:
    model = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    model["services"]["api-production"]["environment"][
        "PRODUCTION_QDRANT_WRITES_ENABLED"
    ] = "true"

    def no_pyyaml() -> object:
        raise ModuleNotFoundError("No module named 'yaml'")

    monkeypatch.setattr(compose_static_check, "_load_with_pyyaml", no_pyyaml)
    monkeypatch.setattr(
        compose_static_check.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps(model), ""
        ),
    )

    with pytest.raises(
        compose_static_check.ComposeCheckError,
        match="enables persistent writes",
    ):
        compose_static_check.validate("demo")


def test_static_delivery_rejects_compose_include_directive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "include:\n  - hidden-compose.yaml\n"
        + COMPOSE.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(compose_static_check, "COMPOSE", compose)

    with pytest.raises(
        compose_static_check.ComposeCheckError,
        match="forbidden include directive",
    ):
        compose_static_check.validate("demo")


def test_compose_has_gated_services_and_no_private_mounts() -> None:
    model = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = model["services"]

    assert services["api"]["environment"]["MAGICFORGE_PROFILE"] == "demo"
    assert services["api-dev"]["environment"]["MAGICFORGE_PROFILE"] == "development"
    dependencies = services["api"]["depends_on"]
    assert dependencies["postgres"]["condition"] == "service_healthy"
    assert dependencies["qdrant"]["condition"] == "service_healthy"
    assert dependencies["migrate"]["condition"] == "service_completed_successfully"
    assert dependencies["seed"]["condition"] == "service_completed_successfully"
    development_dependencies = services["api-dev"]["depends_on"]
    assert development_dependencies["postgres"]["condition"] == "service_healthy"
    assert development_dependencies["qdrant"]["condition"] == "service_healthy"
    assert development_dependencies["migrate"]["condition"] == "service_completed_successfully"
    assert "seed" not in development_dependencies

    serialized = COMPOSE.read_text(encoding="utf-8").casefold()
    assert "research/runs" not in serialized
    assert "env_file:" not in serialized
    assert "./.env:" not in serialized
    assert "magicforge_bootstrap_" not in serialized
    assert services["backup"]["profiles"] == ["ops-backup"]
    assert services["restore"]["profiles"] == ["ops-restore"]
    assert services["api-production"]["profiles"] == ["production"]
    assert (
        services["postgres-production"]["environment"]["POSTGRES_PASSWORD"]
        == "${MAGICFORGE_PRODUCTION_DB_PASSWORD:-}"
    )
    assert services["postgres-production"].get("ports") is None
    assert services["qdrant-production"].get("ports") is None
    assert services["qdrant"]["ports"] == [
        "127.0.0.1:${MAGICFORGE_QDRANT_PORT:-6333}:6333"
    ]
    assert services["api"]["environment"]["MAGICFORGE_DEMO_QDRANT_URL"] == (
        "http://qdrant:6333"
    )
    assert services["api"]["environment"]["EMBEDDING_DIMENSION"] == "384"
    assert services["seed"]["environment"]["EMBEDDING_DIMENSION"] == "384"
    for name in ("api", "api-dev", "api-production"):
        assert "/health/live" in json.dumps(services[name]["healthcheck"])


def test_container_builds_are_locked_explicit_and_non_root() -> None:
    backend = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8")
    frontend = (ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")

    assert "requirements.lock" in backend
    assert "requirements.txt" not in backend
    assert "data/demo" in backend
    assert "research/models.py" in backend
    assert "research/governance.py" in backend
    assert "research/pipeline.py" in backend
    assert "research/runs" not in backend
    assert "USER 10001:10001" in backend
    assert "USER node" in frontend
    assert backend.count(" AS ") >= 3
    assert frontend.count(" AS ") >= 4
    assert "COPY . " not in backend + frontend
    assert "ARG POSTGRES_IMAGE=postgres:16.6-bookworm" in backend
    assert "FROM ${POSTGRES_IMAGE} AS ops" in backend
    assert "postgresql-client" not in backend
    assert "/health/live" in backend


def test_public_demo_seed_validates_offline() -> None:
    environment = dict(os.environ)
    environment["MAGICFORGE_TARGET_PROFILE"] = "demo"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.ops.seed_demo_qdrant",
            "--corpus",
            str(DEMO_CORPUS),
            "--check",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "valid"
    assert report["collection"] == "magicforge_demo_v01"
    assert report["point_count"] > 0


def test_seed_and_backup_guards_reject_production(monkeypatch) -> None:
    from scripts.ops import seed_demo_qdrant

    monkeypatch.setenv("MAGICFORGE_TARGET_PROFILE", "production")
    with pytest.raises(seed_demo_qdrant.DemoSeedError):
        seed_demo_qdrant._profile()

    monkeypatch.setenv("MAGICFORGE_TARGET_PROFILE", "demo")
    monkeypatch.setenv("QDRANT_URL", "https://production-qdrant.example.test:6333")
    with pytest.raises(seed_demo_qdrant.DemoSeedError, match="isolated Compose"):
        seed_demo_qdrant.seed(DEMO_CORPUS)

    monkeypatch.setenv("MAGICFORGE_TARGET_PROFILE", "demo")
    monkeypatch.setenv("MAGICFORGE_OPS_OPERATION", "backup")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("QDRANT_COLLECTION_NAME", "magicforge_knowledge_v01")
    with pytest.raises(backup_restore.OpsSafetyError, match="Production"):
        backup_restore._qdrant()

    monkeypatch.setenv("QDRANT_COLLECTION_NAME", backup_restore.DEMO_COLLECTION)
    monkeypatch.setenv("QDRANT_URL", "https://production-qdrant.example.test:6333")
    with pytest.raises(backup_restore.OpsSafetyError, match="isolated Compose Demo"):
        backup_restore._qdrant()

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://magicforge:magicforge_demo_only@production-db:5432/magicforge",
    )
    with pytest.raises(backup_restore.OpsSafetyError, match="isolated Compose Demo"):
        backup_restore._database_command("pg_restore")


def test_restore_requires_exact_profile_confirmation(monkeypatch) -> None:
    monkeypatch.setenv("MAGICFORGE_TARGET_PROFILE", "demo")
    monkeypatch.setenv("MAGICFORGE_OPS_OPERATION", "restore")
    monkeypatch.setenv("MAGICFORGE_RESTORE_CONFIRM", "restore-production")

    with pytest.raises(backup_restore.OpsSafetyError, match="restore-demo"):
        backup_restore.restore()


def _write_backup_bundle(
    root: Path,
    *,
    identity: dict[str, str],
) -> Path:
    bundle = root / "magicforge-demo-20260811T000000Z"
    bundle.mkdir()
    database_dump = bundle / "postgres.dump"
    qdrant_dump = bundle / "qdrant.snapshot"
    database_dump.write_bytes(b"postgres-demo")
    qdrant_dump.write_bytes(b"qdrant-demo")
    metadata = {
        "schema": backup_restore.BACKUP_SCHEMA,
        "created_at": "2026-08-11T00:00:00+00:00",
        "profile": "demo",
        "collection": backup_restore.DEMO_COLLECTION,
        "qdrant_point_count": 29,
        "postgres_sha256": backup_restore._sha256(database_dump),
        "qdrant_sha256": backup_restore._sha256(qdrant_dump),
        **identity,
    }
    (bundle / "metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )
    return bundle


def test_backup_identity_binds_release_migrations_and_demo_corpus() -> None:
    identity = backup_restore._application_identity()

    assert identity["app_version"] == PUBLIC_VERSION
    assert identity["alembic_revision"] == "20260809_0007"
    assert identity["demo_corpus_id"] == "magicforge-demo-v01"
    assert identity["demo_corpus_schema_version"] == "demo-corpus-0.1"
    assert identity["demo_manifest_id"]
    assert len(identity["demo_manifest_hash"]) == 64
    assert identity["demo_receipt_id"]


def test_backup_rejects_a_database_behind_the_application_head(monkeypatch) -> None:
    monkeypatch.setenv("MAGICFORGE_TARGET_PROFILE", "demo")
    monkeypatch.setenv("MAGICFORGE_OPS_OPERATION", "backup")
    monkeypatch.setattr(
        backup_restore,
        "_application_identity",
        lambda: {"alembic_revision": "20260809_0007"},
    )
    monkeypatch.setattr(backup_restore, "_database_revision", lambda: "20260808_0006")

    with pytest.raises(backup_restore.OpsSafetyError, match="migration head"):
        backup_restore.backup()


def test_restore_bundle_accepts_only_current_application_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    identity = backup_restore._application_identity()
    bundle = _write_backup_bundle(tmp_path, identity=identity)
    monkeypatch.setattr(backup_restore, "BACKUP_ROOT", tmp_path)
    monkeypatch.setenv("MAGICFORGE_RESTORE_SOURCE", bundle.name)

    restored_bundle, metadata = backup_restore._restore_bundle("demo")

    assert restored_bundle == bundle
    assert metadata["demo_manifest_hash"] == identity["demo_manifest_hash"]


def test_restore_bundle_rejects_another_application_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    identity = backup_restore._application_identity()
    identity["app_version"] = "0.1.0-other"
    bundle = _write_backup_bundle(tmp_path, identity=identity)
    monkeypatch.setattr(backup_restore, "BACKUP_ROOT", tmp_path)
    monkeypatch.setenv("MAGICFORGE_RESTORE_SOURCE", bundle.name)

    with pytest.raises(backup_restore.OpsSafetyError, match="application and Demo"):
        backup_restore._restore_bundle("demo")


def test_restore_uses_snapshot_priority_and_checksum(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle = tmp_path / "magicforge-demo-restore"
    bundle.mkdir()
    (bundle / "postgres.dump").write_bytes(b"postgres-demo")
    (bundle / "qdrant.snapshot").write_bytes(b"qdrant-demo")
    metadata = {
        "alembic_revision": "20260809_0007",
        "qdrant_point_count": 29,
        "qdrant_sha256": backup_restore._sha256(bundle / "qdrant.snapshot"),
    }
    observed: dict[str, object] = {}

    class SnapshotsApi:
        def recover_from_uploaded_snapshot(self, **kwargs) -> None:
            observed.update(kwargs)

    class Client:
        http = SimpleNamespace(snapshots_api=SnapshotsApi())

        @staticmethod
        def get_collections() -> SimpleNamespace:
            return SimpleNamespace(collections=[])

        @staticmethod
        def count(*, collection_name: str, exact: bool) -> SimpleNamespace:
            assert collection_name == backup_restore.DEMO_COLLECTION
            assert exact is True
            return SimpleNamespace(count=29)

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setenv("MAGICFORGE_TARGET_PROFILE", "demo")
    monkeypatch.setenv("MAGICFORGE_OPS_OPERATION", "restore")
    monkeypatch.setenv("MAGICFORGE_RESTORE_CONFIRM", "restore-demo")
    monkeypatch.setattr(
        backup_restore,
        "_restore_bundle",
        lambda _profile: (bundle, metadata),
    )
    monkeypatch.setattr(backup_restore, "_run_database", lambda *_args: None)
    monkeypatch.setattr(
        backup_restore,
        "_database_revision",
        lambda: "20260809_0007",
    )
    monkeypatch.setattr(
        backup_restore,
        "_qdrant",
        lambda: (Client(), "http://qdrant:6333", backup_restore.DEMO_COLLECTION),
    )

    backup_restore.restore()

    assert observed["priority"] == backup_restore.models.SnapshotPriority.SNAPSHOT
    assert observed["checksum"] == metadata["qdrant_sha256"]
    assert observed["wait"] is True

    class MismatchClient(Client):
        @staticmethod
        def count(*, collection_name: str, exact: bool) -> SimpleNamespace:
            return SimpleNamespace(count=28)

    monkeypatch.setattr(
        backup_restore,
        "_qdrant",
        lambda: (
            MismatchClient(),
            "http://qdrant:6333",
            backup_restore.DEMO_COLLECTION,
        ),
    )
    with pytest.raises(backup_restore.OpsSafetyError, match="partial and must remain stopped"):
        backup_restore.restore()


def test_release_workflow_reuses_the_complete_ci_gate() -> None:
    ci = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    release = yaml.load(
        (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )

    assert "workflow_call" in ci["on"]
    assert ci["jobs"]["backend"]["services"]["qdrant"]["image"] == (
        "qdrant/qdrant:v1.19.0"
    )
    assert release["jobs"]["full-ci"]["uses"] == "./.github/workflows/ci.yml"
    assert release["jobs"]["source-artifact"]["needs"] == "full-ci"
    assert release["on"]["workflow_dispatch"]["inputs"]["release_tag"]["required"] == "true"
    release_text = (ROOT / ".github/workflows/release.yml").read_text(
        encoding="utf-8"
    )
    assert 'test "$(git rev-list -n 1 "${RELEASE_TAG}")" = "${GITHUB_SHA}"' in release_text
    assert 'check_release_version.py --tag "${RELEASE_TAG}"' in release_text

    lifecycle = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    commands = (
        "./magicforge up demo",
        "./magicforge doctor demo",
        "./magicforge down demo --volumes --confirm delete-demo-volumes",
        "./magicforge up demo",
        "./magicforge doctor demo",
        "./magicforge backup demo",
        "./magicforge down demo --volumes --confirm delete-demo-volumes",
        "./magicforge restore demo",
        "./magicforge up demo",
        "./magicforge doctor demo",
    )
    position = -1
    for command in commands:
        position = lifecycle.index(command, position + 1)


def test_cli_static_doctor_and_fail_closed_paths() -> None:
    help_result = _run_cli("--help")
    source_audit = _run_cli("audit-public")
    static_result = _run_cli("doctor", "demo", "--static")
    unavailable_result = _run_cli(
        "doctor", "demo", docker_bin="/definitely/unavailable/docker"
    )
    unsafe_down = _run_cli(
        "down", "demo", "--volumes", docker_bin="/definitely/unavailable/docker"
    )
    unsafe_development_backup = _run_cli(
        "backup",
        "development",
        "--output",
        "/tmp/magicforge-forbidden-development-backup",
        docker_bin="/definitely/unavailable/docker",
    )

    assert help_result.returncode == 0
    assert "audit-public" in help_result.stdout
    assert "build-public-release" in help_result.stdout
    assert source_audit.returncode == 0, source_audit.stderr
    assert static_result.returncode == 0, static_result.stderr
    assert "static doctor passed" in static_result.stdout
    assert unavailable_result.returncode != 0
    assert "Docker CLI is unavailable" in unavailable_result.stderr
    assert unsafe_down.returncode != 0
    assert "delete-demo-volumes" in unsafe_down.stderr
    assert unsafe_development_backup.returncode != 0
    assert "public Demo profile" in unsafe_development_backup.stderr
    launcher = CLI.read_text(encoding="utf-8")
    assert "wget --quiet --output-document=/dev/null" in launcher
    assert "wget --quiet --spider" not in launcher
    assert 'chmod 0755 -- "${stage}"' in launcher


def test_cli_up_waits_for_runtime_health_and_completed_jobs(tmp_path: Path) -> None:
    docker = _write_fake_docker(tmp_path)
    result = _run_cli(
        "up",
        "demo",
        docker_bin=str(docker),
        extra_environment={
            "FAKE_DOCKER_STATE_DIR": str(tmp_path),
            "MAGICFORGE_STARTUP_TIMEOUT_SECONDS": "5",
            "MAGICFORGE_STARTUP_POLL_SECONDS": "0.01",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "MagicForge demo started" in result.stdout
    assert (tmp_path / "web-inspections").read_text(encoding="utf-8").strip() == "2"
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "--project-name magicforge-demo" in calls
    assert "inspect --format" in calls
    assert "seed-container" in calls
    assert "migrate-container" in calls


def test_destructive_volume_cleanup_is_isolated_by_profile(tmp_path: Path) -> None:
    docker = _write_fake_docker(tmp_path)
    environment = {"FAKE_DOCKER_STATE_DIR": str(tmp_path)}

    demo = _run_cli(
        "down",
        "demo",
        "--volumes",
        "--confirm",
        "delete-demo-volumes",
        docker_bin=str(docker),
        extra_environment=environment,
    )
    development = _run_cli(
        "down",
        "development",
        "--volumes",
        "--confirm",
        "delete-development-volumes",
        docker_bin=str(docker),
        extra_environment=environment,
    )

    assert demo.returncode == 0, demo.stderr
    assert development.returncode == 0, development.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "--project-name magicforge-demo" in calls
    assert "--project-name magicforge-development" in calls
    assert calls.count("down --remove-orphans --volumes") == 2


def test_demo_backup_and_restore_path_guards_need_no_host_python(
    tmp_path: Path,
) -> None:
    docker = _write_fake_docker(tmp_path)
    backup_root = tmp_path / "backups"
    bundle = backup_root / "magicforge-demo-fixture"
    bundle.mkdir(parents=True)
    environment = {
        "FAKE_DOCKER_STATE_DIR": str(tmp_path),
        "MAGICFORGE_PYTHON_BIN": "/definitely/unavailable/python",
    }

    backup = _run_cli(
        "backup",
        "demo",
        "--output",
        str(backup_root),
        docker_bin=str(docker),
        extra_environment=environment,
    )
    restore = _run_cli(
        "restore",
        "demo",
        "--input",
        str(bundle),
        "--confirm",
        "restore-demo",
        docker_bin=str(docker),
        extra_environment=environment,
    )

    assert backup.returncode == 0, backup.stderr
    assert restore.returncode == 0, restore.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8")
    assert "--project-name magicforge-demo" in calls
    assert "/definitely/unavailable/python" not in calls


def test_cli_up_fails_when_seed_job_fails(tmp_path: Path) -> None:
    docker = _write_fake_docker(tmp_path)
    result = _run_cli(
        "up",
        "demo",
        docker_bin=str(docker),
        extra_environment={
            "FAKE_DOCKER_STATE_DIR": str(tmp_path),
            "FAKE_DOCKER_SEED_EXIT": "17",
            "MAGICFORGE_STARTUP_TIMEOUT_SECONDS": "5",
            "MAGICFORGE_STARTUP_POLL_SECONDS": "0.01",
        },
    )

    assert result.returncode != 0
    assert "seed failed during startup (exit=17)" in result.stderr
    assert "MagicForge demo started" not in result.stdout


def test_cli_doctor_reads_qdrant_inside_the_compose_network(tmp_path: Path) -> None:
    docker, _curl = _write_fake_runtime_doctor(tmp_path)
    result = _run_cli(
        "doctor",
        "development",
        docker_bin=str(docker),
        extra_environment={
            "FAKE_DOCKER_STATE_DIR": str(tmp_path),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "doctor: development is ready" in result.stdout
    calls = (tmp_path / "doctor-calls.log").read_text(encoding="utf-8")
    assert (
        "exec --no-TTY --env PYTHONDONTWRITEBYTECODE=1 "
        "api-dev python -m scripts.ops.doctor"
    ) in calls
    assert "--api-url http://127.0.0.1:8000" in calls
    assert "--qdrant-url http://qdrant:6333" in calls
    assert "--corpus /opt/magicforge/data/demo/corpus.json" in calls
    assert "127.0.0.1:6333" not in calls
    curl_calls = (tmp_path / "curl-calls.log").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8000/health/ready" in curl_calls
    assert "http://127.0.0.1:3000/login" in curl_calls


def test_demo_doctor_needs_no_host_python_and_runs_smoke_in_api(
    tmp_path: Path,
) -> None:
    docker, model_path = _write_clean_host_docker(tmp_path)
    result = _run_cli(
        "doctor",
        "demo",
        docker_bin=str(docker),
        extra_environment={
            "FAKE_DOCKER_STATE_DIR": str(tmp_path),
            "FAKE_COMPOSE_MODEL": str(model_path),
            "MAGICFORGE_PYTHON_BIN": "/definitely/unavailable/python",
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "static doctor passed for demo" in result.stdout
    assert "doctor: demo is ready" in result.stdout
    calls = (tmp_path / "clean-host-calls.log").read_text(encoding="utf-8")
    assert "config --format json --no-interpolate" in calls
    assert "python:3.12.3-slim-bookworm python -m scripts.ops.compose_static_check" in calls
    assert "--network none" in calls
    assert "--read-only" in calls
    assert f"source={ROOT}" not in calls
    assert ".env" not in calls
    assert (
        "api python -m scripts.ops.full_stack_smoke --web-url http://web:3000"
        in calls
    )
    assert "/definitely/unavailable/python" not in calls
    http_calls = (tmp_path / "clean-host-http.log").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8000/health/ready" in http_calls
    assert "http://127.0.0.1:3000/login" in http_calls


def test_unsupported_host_python_falls_back_to_locked_container(
    tmp_path: Path,
) -> None:
    docker, model_path = _write_clean_host_docker(tmp_path)
    old_python = tmp_path / "python-old"
    old_python.write_text(
        "#!/usr/bin/env bash\nprintf 'probed\\n' >> \"${FAKE_DOCKER_STATE_DIR:?}/old-python.log\"\nexit 1\n",
        encoding="utf-8",
    )
    old_python.chmod(0o755)

    result = _run_cli(
        "doctor",
        "demo",
        docker_bin=str(docker),
        extra_environment={
            "FAKE_DOCKER_STATE_DIR": str(tmp_path),
            "FAKE_COMPOSE_MODEL": str(model_path),
            "MAGICFORGE_PYTHON_BIN": str(old_python),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
        },
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "old-python.log").read_text(encoding="utf-8") == "probed\n"
    calls = (tmp_path / "clean-host-calls.log").read_text(encoding="utf-8")
    assert "python:3.12.3-slim-bookworm python -m scripts.ops.compose_static_check" in calls


def test_demo_up_needs_no_host_python_and_keeps_static_check_first(
    tmp_path: Path,
) -> None:
    docker, model_path = _write_clean_host_docker(tmp_path)
    result = _run_cli(
        "up",
        "demo",
        docker_bin=str(docker),
        extra_environment={
            "FAKE_DOCKER_STATE_DIR": str(tmp_path),
            "FAKE_COMPOSE_MODEL": str(model_path),
            "MAGICFORGE_PYTHON_BIN": "/definitely/unavailable/python",
            "MAGICFORGE_STARTUP_TIMEOUT_SECONDS": "5",
            "MAGICFORGE_STARTUP_POLL_SECONDS": "0.01",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "static doctor passed for demo" in result.stdout
    assert "MagicForge demo started" in result.stdout
    calls = (tmp_path / "clean-host-calls.log").read_text(encoding="utf-8")
    assert calls.index("config --format json --no-interpolate") < calls.index(
        "up --detach --build"
    )
    assert "/definitely/unavailable/python" not in calls


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        (
            "MAGICFORGE_STARTUP_TIMEOUT_SECONDS",
            "not-a-timeout",
            "must be an integer from 1 to 900",
        ),
        (
            "MAGICFORGE_STARTUP_TIMEOUT_SECONDS",
            "901",
            "must be an integer from 1 to 900",
        ),
        (
            "MAGICFORGE_STARTUP_POLL_SECONDS",
            "later",
            "must be greater than 0 and at most 30",
        ),
        (
            "MAGICFORGE_STARTUP_POLL_SECONDS",
            "0",
            "must be greater than 0 and at most 30",
        ),
        (
            "MAGICFORGE_STARTUP_POLL_SECONDS",
            "30.1",
            "must be greater than 0 and at most 30",
        ),
    ],
)
def test_cli_up_rejects_invalid_wait_configuration_before_docker_side_effects(
    tmp_path: Path,
    variable: str,
    value: str,
    message: str,
) -> None:
    docker = _write_fake_docker(tmp_path)
    result = _run_cli(
        "up",
        "demo",
        docker_bin=str(docker),
        extra_environment={
            "FAKE_DOCKER_STATE_DIR": str(tmp_path),
            variable: value,
        },
    )

    assert result.returncode != 0
    assert message in result.stderr
    calls_path = tmp_path / "calls.log"
    assert not calls_path.exists() or " up " not in calls_path.read_text(encoding="utf-8")


def test_cli_positional_audit_target_is_treated_as_artifact(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Public artifact\n", encoding="utf-8")

    result = _run_cli("audit-public", str(tmp_path), "--json")

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report["ok"] is False
    assert {item["code"] for item in report["findings"]} == {
        "release_manifest_missing"
    }


def test_runtime_doctor_checks_all_demo_identities(monkeypatch) -> None:
    payload = json.loads(DEMO_CORPUS.read_text(encoding="utf-8"))
    points = sum(len(payload[name]) for name in ("claims", "nodes", "relationships"))
    responses = {
        "http://api/health/ready": {
            "status": "ready",
            "mode": "bootstrap",
            "profile": "demo",
            "read_only": True,
            "synthetic_corpus": True,
            "corpus_id": "magicforge-demo-v01",
            "manifest_schema_version": "demo-corpus-0.1",
            "collection": "magicforge_demo_v01",
            "storage_kind": "remote",
            "glm_configured": False,
        },
        "http://api/research/console": {
            "current_run": {
                "run_id": "magicforge-demo-v01",
                "collection": "magicforge_demo_v01",
            },
            "runtime": {
                "retrieval": {"collection": "magicforge_demo_v01"},
                "intelligence_instrument": {
                    "provider": "Offline deterministic Demo"
                },
            },
            "memory_vault": {
                "runtime_collection": "magicforge_demo_v01",
                "audited_collection": "magicforge_demo_v01",
                "manifest": {"point_count": points},
                "receipt": {"point_count": points},
            },
        },
        "http://qdrant/collections/magicforge_demo_v01": {
            "result": {"points_count": points}
        },
    }
    monkeypatch.setattr(doctor, "_read_json_url", responses.__getitem__)

    report = doctor.verify(
        api_url="http://api",
        qdrant_url="http://qdrant",
        corpus_path=DEMO_CORPUS,
    )

    assert report["point_count"] == points
    assert report["private_mounts"] == 0


def test_full_stack_smoke_crosses_pages_and_bff(monkeypatch) -> None:
    pages: list[str] = []

    def fake_page(url: str) -> None:
        pages.append(url)

    def fake_json(url: str, *, method: str = "GET", payload=None):
        if url.endswith("/api/magicforge/health"):
            return {
                "profile": "demo",
                "read_only": True,
                "synthetic_corpus": True,
                "glm_configured": False,
            }
        if url.endswith("/api/magicforge/stats"):
            return {"qdrant_points": 29}
        if "/api/magicforge/knowledge/search?" in url:
            return {
                "evidence_cards": [{"id": "card-1"}],
                "nodes": [{"id": "node-1"}],
                "relationships": [{"id": "relationship-1"}],
            }
        if url.endswith("/api/magicforge/evidence/card-1"):
            return {"id": "card-1"}
        if url.endswith("/api/magicforge/chat"):
            assert method == "POST"
            assert payload == {
                "question": "How is attention represented in this Demo?"
            }
            return {"result": "offline answer", "sources": [{"title": "Demo"}]}
        if url.endswith("/api/magicforge/research/console"):
            return {
                "runtime": {
                    "intelligence_instrument": {
                        "provider": "Offline deterministic Demo"
                    }
                }
            }
        raise AssertionError(url)

    monkeypatch.setattr(full_stack_smoke, "_request_page", fake_page)
    monkeypatch.setattr(full_stack_smoke, "_request_json", fake_json)

    report = full_stack_smoke.verify("http://web")

    assert report == {
        "status": "passed",
        "pages": 4,
        "evidence_cards": 1,
        "nodes": 1,
        "relationships": 1,
        "qdrant_points": 29,
    }
    assert pages == [
        "http://web/",
        "http://web/dashboard",
        "http://web/evidence",
        "http://web/knowledge",
    ]


def test_runtime_doctor_keeps_development_distinct_from_demo(monkeypatch) -> None:
    responses = {
        "http://api/health/ready": {
            "status": "ready",
            "mode": "bootstrap",
            "profile": "development",
            "read_only": False,
            "synthetic_corpus": False,
            "corpus_id": "reviewed-development-corpus",
            "collection": "magicforge_development_v01",
        },
        "http://api/research/console": {
            "current_run": {
                "run_id": "reviewed-development-corpus",
                "collection": "magicforge_development_v01",
            },
            "memory_vault": {
                "runtime_collection": "magicforge_development_v01",
                "manifest": {"point_count": 17},
                "receipt": {"point_count": 17},
            },
        },
        "http://qdrant/collections/magicforge_development_v01": {
            "result": {"points_count": 17}
        },
    }
    monkeypatch.setattr(doctor, "_read_json_url", responses.__getitem__)

    report = doctor.verify_development(
        api_url="http://api",
        qdrant_url="http://qdrant",
    )

    assert report["profile"] == "development"
    assert report["point_count"] == 17


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_docker_compose_resolves_when_cli_is_available() -> None:
    availability = subprocess.run(
        ["docker", "compose", "version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if availability.returncode != 0:
        pytest.skip("Docker Desktop WSL integration is unavailable")
    result = subprocess.run(
        ["docker", "compose", "--file", str(COMPOSE), "--profile", "demo", "config", "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
