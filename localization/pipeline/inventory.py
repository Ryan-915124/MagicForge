"""Bridge to the read-only TypeScript localization inventory."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class InventoryError(RuntimeError):
    """Raised when the frontend inventory cannot be produced safely."""


def collect_frontend_inventory(
    project_root: Path,
    *,
    node_binary: str = "node",
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    script_path = project_root / "frontend/scripts/localization-inventory.mjs"
    if not script_path.is_file():
        raise InventoryError(f"localization inventory script not found: {script_path}")

    try:
        completed = subprocess.run(
            [node_binary, str(script_path)],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise InventoryError(
            f"Node.js executable {node_binary!r} was not found; install Node.js to scan the TypeScript catalog"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise InventoryError(
            f"frontend localization inventory exceeded {timeout_seconds:g} seconds"
        ) from exc

    try:
        inventory = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        diagnostic = completed.stderr.strip() or "inventory emitted invalid JSON"
        raise InventoryError(diagnostic) from exc

    if not isinstance(inventory, dict):
        raise InventoryError("frontend localization inventory must be a JSON object")
    if completed.returncode != 0 or not inventory.get("ok"):
        errors = inventory.get("catalogValidation", {}).get("errors", [])
        diagnostic = "; ".join(str(error) for error in errors)
        if not diagnostic:
            diagnostic = completed.stderr.strip() or "frontend inventory validation failed"
        raise InventoryError(diagnostic)
    catalogs = inventory.get("catalogs")
    if not isinstance(catalogs, dict) or not isinstance(catalogs.get("enUS"), dict):
        raise InventoryError("frontend inventory does not contain an enUS canonical catalog")
    if not isinstance(catalogs.get("zhCN"), dict):
        raise InventoryError("frontend inventory does not contain a zhCN catalog")
    return inventory
