"""Load and fingerprint every existing MagicForge localization asset."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


REQUIRED_ASSETS = (
    "localization/glossary.en-zh.yaml",
    "localization/do-not-translate.yaml",
    "localization/translation-policy.md",
    "localization/brand-voice.md",
    "localization/translation-memory.jsonl",
    "localization/prompts/ui-localizer-v1.md",
    "localization/prompts/ui-quality-reviewer-v1.md",
)


class LocalizationAssetError(RuntimeError):
    """Raised when a required local asset is absent or internally invalid."""


def load_localization_asset_manifest(project_root: Path) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    for relative_path in REQUIRED_ASSETS:
        path = project_root / relative_path
        if not path.is_file():
            raise LocalizationAssetError(f"required localization asset not found: {path}")
        payload = path.read_bytes()
        if not payload:
            raise LocalizationAssetError(f"required localization asset is empty: {path}")
        files[relative_path] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }

    memory_path = project_root / "localization/translation-memory.jsonl"
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(
        memory_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise LocalizationAssetError(
                f"invalid Translation Memory JSON at {memory_path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise LocalizationAssetError(
                f"Translation Memory record at {memory_path}:{line_number} must be an object"
            )
        records.append(record)

    if not records or records[0].get("record_type") != "metadata":
        raise LocalizationAssetError("Translation Memory must start with one metadata record")
    metadata = records[0]
    units = records[1:]
    declared_count = metadata.get("translation_unit_count")
    if declared_count != len(units):
        raise LocalizationAssetError(
            "Translation Memory metadata count does not match its translation-unit records"
        )
    for position, unit in enumerate(units, 2):
        if unit.get("record_type") != "translation_unit":
            raise LocalizationAssetError(
                f"Translation Memory line {position} has an unsupported record_type"
            )
        if unit.get("status") != "approved":
            raise LocalizationAssetError(
                f"Translation Memory line {position} is not explicitly approved"
            )
        if unit.get("search_alias") or unit.get("unit_kind") == "search_alias":
            raise LocalizationAssetError(
                f"Translation Memory line {position} contains a forbidden search alias"
            )

    approved_count = sum(unit.get("status") == "approved" for unit in units)
    if metadata.get("approved_translation_units") != approved_count:
        raise LocalizationAssetError(
            "Translation Memory metadata approved count does not match its records"
        )
    if metadata.get("search_alias_units") != 0:
        raise LocalizationAssetError(
            "Translation Memory metadata must declare zero search-alias units"
        )

    return {
        "schema_version": str(metadata.get("schema_version", "")),
        "source_locale": metadata.get("source_locale"),
        "target_locale": metadata.get("target_locale"),
        "files": files,
        "translation_memory": {
            "status": metadata.get("status"),
            "translation_unit_count": len(units),
            "approved_translation_units": approved_count,
        },
    }
