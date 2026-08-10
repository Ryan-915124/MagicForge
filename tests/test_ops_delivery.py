from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from scripts.ops import backup_restore, compose_static_check, doctor, full_stack_smoke


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "magicforge"
COMPOSE = ROOT / "compose.yaml"
DEMO_CORPUS = ROOT / "data/demo/corpus.json"
PUBLIC_VERSION = "0.1.0-alpha.2"


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
    monkeypatch.setenv("MAGICFORGE_OPS_OPERATION", "backup")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("QDRANT_COLLECTION_NAME", "magicforge_knowledge_v01")
    with pytest.raises(backup_restore.OpsSafetyError, match="Production"):
        backup_restore._qdrant()


def test_restore_requires_exact_profile_confirmation(monkeypatch) -> None:
    monkeypatch.setenv("MAGICFORGE_TARGET_PROFILE", "demo")
    monkeypatch.setenv("MAGICFORGE_OPS_OPERATION", "restore")
    monkeypatch.setenv("MAGICFORGE_RESTORE_CONFIRM", "restore-production")

    with pytest.raises(backup_restore.OpsSafetyError, match="restore-demo"):
        backup_restore.restore()


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
    assert "inspect --format" in calls
    assert "seed-container" in calls
    assert "migrate-container" in calls


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
            "storage_kind": "local",
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
