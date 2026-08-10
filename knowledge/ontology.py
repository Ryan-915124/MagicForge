"""Shared ontology path normalization and validation."""

from __future__ import annotations

import re


def normalize_ontology_path(value: str) -> str:
    """Return one canonical dot path and fail closed on legacy slash paths."""

    raw = value.strip()
    if not raw:
        raise ValueError("ontology path cannot be blank")
    if "/" in raw:
        raise ValueError("ontology paths must use dot notation; slash paths are rejected")
    segments = []
    for segment in raw.split("."):
        snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", segment)
        normalized = re.sub(r"[^a-z0-9]+", "_", snake_case.casefold()).strip("_")
        if not normalized:
            raise ValueError("ontology path contains an empty segment")
        segments.append(normalized)
    if len(segments) < 2:
        raise ValueError("ontology path must contain at least two dot-separated segments")
    return ".".join(segments)


def normalize_ontology_paths(values: list[str]) -> list[str]:
    return list(dict.fromkeys(normalize_ontology_path(value) for value in values))


def canonical_entity_ontology_path(entity_type: str, name: str) -> str:
    """Build an entity-owned path instead of reusing a source-topic path."""

    roots = {
        "effect": "magic.effect",
        "technique": "magic.technique",
        "method": "magic.method",
        "performer": "magic.performer",
        "psychology_principle": "psychology.principle",
        "cognitive_mechanism": "psychology.cognitive_mechanism",
        "source": "provenance.source",
        "research_paper": "provenance.research_paper",
    }
    try:
        root = roots[entity_type]
    except KeyError as exc:
        raise ValueError(f"unsupported entity type for ontology path: {entity_type}") from exc
    slug = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
    if not slug:
        raise ValueError("entity name cannot produce an ontology path")
    return normalize_ontology_path(f"{root}.{slug}")


__all__ = [
    "canonical_entity_ontology_path",
    "normalize_ontology_path",
    "normalize_ontology_paths",
]
