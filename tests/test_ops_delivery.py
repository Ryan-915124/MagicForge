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


def _run_cli(*arguments: str, docker_bin: str | None = None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    if docker_bin is not None:
        environment["MAGICFORGE_DOCKER_BIN"] = docker_bin
    return subprocess.run(
        ["bash", str(CLI), *arguments],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("profile", ["demo", "development", "production"])
def test_static_delivery_check_passes(profile: str) -> None:
    compose_static_check.validate(profile)


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
