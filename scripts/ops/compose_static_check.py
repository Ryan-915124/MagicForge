"""Read-only structural validation for MagicForge Compose delivery."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose.yaml"
ALLOWED_PROFILES = frozenset({"demo", "development", "production"})
DEMO_CORPUS_ID = "magicforge-demo-v01"
DEMO_COLLECTION = "magicforge_demo_v01"


class ComposeCheckError(RuntimeError):
    pass


def _load_with_pyyaml() -> object:
    import yaml

    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _load_with_compose_cli() -> object:
    docker_bin = os.environ.get("MAGICFORGE_DOCKER_BIN", "docker")
    with tempfile.TemporaryDirectory(prefix="magicforge-compose-check-") as directory:
        empty_environment = Path(directory) / "empty.env"
        empty_environment.write_text("", encoding="utf-8")
        command = [
            docker_bin,
            "compose",
            "--env-file",
            str(empty_environment),
            "--file",
            str(COMPOSE),
            "--profile",
            "*",
            "config",
            "--format",
            "json",
            "--no-interpolate",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ComposeCheckError(
                "compose.yaml validation requires Docker Compose when PyYAML is unavailable"
            ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise ComposeCheckError(f"Docker Compose could not render compose.yaml{suffix}")
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ComposeCheckError("Docker Compose returned invalid JSON") from exc


def _load_model() -> object:
    try:
        return _load_with_pyyaml()
    except ModuleNotFoundError:
        return _load_with_compose_cli()
    except Exception as exc:
        raise ComposeCheckError("compose.yaml is not valid YAML") from exc


def _load_json_model(payload: str) -> object:
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ComposeCheckError("Docker Compose returned invalid JSON") from exc


def validate(profile: str, *, model: object | None = None) -> None:
    if profile not in ALLOWED_PROFILES:
        raise ComposeCheckError("profile must be demo, development, or production")
    if model is None:
        model = _load_model()
    services = model.get("services") if isinstance(model, dict) else None
    if not isinstance(services, dict):
        raise ComposeCheckError("compose.yaml has no services mapping")

    expected = {
        "demo": {"postgres", "qdrant", "migrate", "seed", "api", "web"},
        "development": {
            "postgres",
            "qdrant",
            "migrate",
            "api-dev",
            "web-dev",
        },
        "production": {
            "postgres-production",
            "qdrant-production",
            "migrate-production",
            "api-production",
            "web-production",
        },
    }[profile]
    missing = sorted(expected - set(services))
    if missing:
        raise ComposeCheckError(f"missing services: {', '.join(missing)}")

    for name in (
        "postgres",
        "qdrant",
        "api",
        "web",
        "api-dev",
        "web-dev",
        "postgres-production",
        "qdrant-production",
        "api-production",
        "web-production",
    ):
        if "healthcheck" not in services[name]:
            raise ComposeCheckError(f"{name} has no healthcheck")
    for name in ("postgres", "qdrant", "postgres-production", "qdrant-production"):
        image = str(services[name].get("image") or "")
        if not image or ":" not in image or image.endswith(":latest"):
            raise ComposeCheckError(f"{name} image is not version-pinned")

    for api_name in ("api",):
        environment = services[api_name].get("environment") or {}
        if environment.get("MAGICFORGE_PROFILE") != "demo":
            raise ComposeCheckError(f"{api_name} is not isolated to the Demo profile")
        if environment.get("GLM_API_KEY") not in {"", None}:
            raise ComposeCheckError(f"{api_name} exposes an LLM credential")
        if environment.get("MAGICFORGE_DEMO_QDRANT_URL") != "http://qdrant:6333":
            raise ComposeCheckError("api does not use the Compose Demo Qdrant service")
        if environment.get("EMBEDDING_DIMENSION") != "384":
            raise ComposeCheckError("api Demo embedding dimension is incompatible")
    seed_environment = services["seed"].get("environment") or {}
    if (
        seed_environment.get("QDRANT_URL") != "http://qdrant:6333"
        or seed_environment.get("EMBEDDING_DIMENSION") != "384"
    ):
        raise ComposeCheckError("Demo seed and API retrieval contracts disagree")
    development_environment = services["api-dev"].get("environment") or {}
    if development_environment.get("MAGICFORGE_PROFILE") != "development":
        raise ComposeCheckError("api-dev is disguised as a Demo runtime")
    if development_environment.get("ACTIVE_CORPUS_ROOT") != (
        "${MAGICFORGE_DEVELOPMENT_CORPUS_ROOT:-"
        "/var/lib/magicforge/development-corpus/not-configured}"
    ):
        raise ComposeCheckError("api-dev does not fail closed without an explicit corpus")
    production_environment = services["api-production"].get("environment") or {}
    if production_environment.get("MAGICFORGE_PROFILE") != "production":
        raise ComposeCheckError("api-production does not select Production")
    if production_environment.get("PRODUCTION_QDRANT_WRITES_ENABLED") != "false":
        raise ComposeCheckError("api-production enables persistent writes by default")
    if production_environment.get("ACTIVE_CORPUS_ID") != "${MAGICFORGE_PRODUCTION_CORPUS_ID:-}":
        raise ComposeCheckError("api-production has a default corpus identity")

    for api_name in ("api",):
        dependencies = services[api_name].get("depends_on") or {}
        required = {
            "postgres": "service_healthy",
            "qdrant": "service_healthy",
            "migrate": "service_completed_successfully",
            "seed": "service_completed_successfully",
        }
        for dependency, condition in required.items():
            if (dependencies.get(dependency) or {}).get("condition") != condition:
                raise ComposeCheckError(
                    f"{api_name} does not gate on {dependency}:{condition}"
                )
    development_dependencies = services["api-dev"].get("depends_on") or {}
    for dependency, condition in {
        "postgres": "service_healthy",
        "qdrant": "service_healthy",
        "migrate": "service_completed_successfully",
    }.items():
        if (development_dependencies.get(dependency) or {}).get("condition") != condition:
            raise ComposeCheckError(
                f"api-dev does not gate on {dependency}:{condition}"
            )
    production_dependencies = services["api-production"].get("depends_on") or {}
    for dependency, condition in {
        "postgres-production": "service_healthy",
        "qdrant-production": "service_healthy",
        "migrate-production": "service_completed_successfully",
    }.items():
        if (production_dependencies.get(dependency) or {}).get("condition") != condition:
            raise ComposeCheckError(
                f"api-production does not gate on {dependency}:{condition}"
            )
    if (
        (services["web"].get("depends_on") or {}).get("api", {}).get("condition")
        != "service_healthy"
        or (services["web-dev"].get("depends_on") or {})
        .get("api-dev", {})
        .get("condition")
        != "service_healthy"
        or (services["web-production"].get("depends_on") or {})
        .get("api-production", {})
        .get("condition")
        != "service_healthy"
    ):
        raise ComposeCheckError("web services do not wait for healthy APIs")

    for name in ("api", "api-dev", "api-production"):
        healthcheck = services[name].get("healthcheck") or {}
        serialized_healthcheck = json.dumps(healthcheck, sort_keys=True)
        if "/health/live" not in serialized_healthcheck:
            raise ComposeCheckError(
                f"{name} healthcheck blocks the governance control plane on corpus readiness"
            )

    raw_compose = COMPOSE.read_text(encoding="utf-8")
    serialized = raw_compose.casefold()
    if re.search(r"(?mi)^[ \t]*include[ \t]*:", raw_compose):
        raise ComposeCheckError("compose.yaml uses a forbidden include directive")
    forbidden_content = (
        "research/runs",
        "magicforge_bootstrap_",
    )
    if any(value in serialized for value in forbidden_content):
        raise ComposeCheckError("compose.yaml references a private corpus boundary")
    if "env_file:" in serialized or "./.env:" in serialized:
        raise ComposeCheckError("compose.yaml imports or mounts a local environment file")
    for service_name, service in services.items():
        for volume in service.get("volumes") or []:
            source = ""
            if isinstance(volume, str):
                source = volume.split(":", 1)[0]
            elif isinstance(volume, dict) and volume.get("type") == "bind":
                source = str(volume.get("source") or "")
            normalized = source.replace("\\", "/").casefold().rstrip("/")
            if normalized in {".", "./", "${pwd}"} or "research/runs" in normalized:
                raise ComposeCheckError(
                    f"{service_name} mounts a forbidden source path"
                )
    if services["backup"].get("profiles") != ["ops-backup"]:
        raise ComposeCheckError("backup is not isolated behind ops-backup")
    if services["restore"].get("profiles") != ["ops-restore"]:
        raise ComposeCheckError("restore is not isolated behind ops-restore")
    if services["seed"].get("profiles") != ["demo"]:
        raise ComposeCheckError("public Demo seed is enabled outside Demo")
    for name in ("backup", "restore"):
        environment = services[name].get("environment") or {}
        if environment.get("QDRANT_COLLECTION_NAME") != DEMO_COLLECTION:
            raise ComposeCheckError(f"{name} can target a non-Demo collection")
    production_database = services["postgres-production"].get("environment") or {}
    if production_database.get("POSTGRES_PASSWORD") != "${MAGICFORGE_PRODUCTION_DB_PASSWORD:-}":
        raise ComposeCheckError("Production PostgreSQL has a default password")
    if services["postgres-production"].get("ports") or services["qdrant-production"].get("ports"):
        raise ComposeCheckError("Production data services publish host ports")

    for dockerfile in (ROOT / "Dockerfile.backend", ROOT / "frontend/Dockerfile"):
        text = dockerfile.read_text(encoding="utf-8")
        if "FROM " not in text or " AS " not in text or "USER " not in text:
            raise ComposeCheckError(f"{dockerfile.name} lacks multi-stage/non-root markers")
        if "COPY . " in text or "COPY .\n" in text or "COPY .env" in text:
            raise ComposeCheckError(f"{dockerfile.name} copies an unsafe build context")
        if "research/runs" in text.casefold():
            raise ComposeCheckError(f"{dockerfile.name} includes private research runs")
    backend_dockerfile = (ROOT / "Dockerfile.backend").read_text(encoding="utf-8")
    if "requirements.lock" not in backend_dockerfile or "data/demo" not in backend_dockerfile:
        raise ComposeCheckError("backend image lacks locked dependencies or public Demo data")
    if (
        "ARG POSTGRES_IMAGE=postgres:16.6-bookworm" not in backend_dockerfile
        or "FROM ${POSTGRES_IMAGE} AS ops" not in backend_dockerfile
    ):
        raise ComposeCheckError("ops image PostgreSQL client does not match the Demo server")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    if (
        "research/runs" not in dockerignore
        or not any(value in dockerignore for value in ("!data/demo", "!/data/demo"))
        or not any(
            value in dockerignore for value in ("!data/demo/**", "!/data/demo/**")
        )
    ):
        raise ComposeCheckError("Docker build-context data boundaries are incomplete")

    try:
        demo = json.loads((ROOT / "data/demo/corpus.json").read_text(encoding="utf-8"))
    except Exception as exc:
        raise ComposeCheckError("the public Demo corpus is invalid JSON") from exc
    if (
        demo.get("corpus_id") != DEMO_CORPUS_ID
        or demo.get("collection_name") != DEMO_COLLECTION
    ):
        raise ComposeCheckError("the public Demo corpus identity is incompatible")
    for marker in ("synthetic", "self_authored", "redistribution_allowed"):
        if demo.get(marker) is not True:
            raise ComposeCheckError(f"the public Demo corpus lacks {marker}=true")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=sorted(ALLOWED_PROFILES))
    parser.add_argument(
        "--compose-json-stdin",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    arguments = parser.parse_args()
    try:
        model = _load_json_model(sys.stdin.read()) if arguments.compose_json_stdin else None
        validate(arguments.profile, model=model)
    except ComposeCheckError as exc:
        print(f"static doctor failed: {exc}", file=sys.stderr)
        return 1
    print(f"static doctor passed for {arguments.profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
