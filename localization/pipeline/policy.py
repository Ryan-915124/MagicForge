"""Load MagicForge localization YAML assets into a normalized policy index."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from localization.pipeline.models import (
    GovernedTerm,
    LocalizationAction,
    PolicyIndex,
    TermStatus,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GLOSSARY_PATH = PROJECT_ROOT / "localization/glossary.en-zh.yaml"
DEFAULT_DO_NOT_TRANSLATE_PATH = PROJECT_ROOT / "localization/do-not-translate.yaml"

# These phrases are explicit brand-voice prohibitions. They stay local and
# deterministic; no translation platform or model is consulted to enforce them.
FORBIDDEN_TARGET_PHRASES = (
    "魔法",
    "召唤答案",
    "神谕",
    "赋能",
    "开启无限可能",
    "智能升级体验",
    "重新定义可能",
    "绝对正确",
    "真相就是",
)


class PolicyConfigurationError(RuntimeError):
    """Raised when a governance asset is missing or internally inconsistent."""


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PolicyConfigurationError(f"localization policy asset not found: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyConfigurationError(f"cannot read localization policy asset {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise PolicyConfigurationError(f"localization policy asset must be a mapping: {path}")
    return loaded


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PolicyConfigurationError(f"{field} must be a list")
    if not all(isinstance(item, str) and item for item in value):
        raise PolicyConfigurationError(f"{field} must contain non-empty strings")
    return tuple(value)


def _mapping(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyConfigurationError(f"{field} must be a mapping")
    return value


def _action_for_display_mode(display_mode: str) -> LocalizationAction:
    actions = {
        "english_only": LocalizationAction.PRESERVE_ENGLISH_ONLY,
        "limited_bilingual": LocalizationAction.LIMITED_BILINGUAL_SUPPORT,
        "localized_ui": LocalizationAction.NORMAL_UI_TRANSLATION,
        "product_name_review": LocalizationAction.PRESERVE_ENGLISH_ONLY,
        "source_original": LocalizationAction.PRESERVE_EXACT,
    }
    try:
        return actions[display_mode]
    except KeyError as exc:
        raise PolicyConfigurationError(f"unknown glossary display mode: {display_mode}") from exc


def load_policy_index(
    glossary_path: str | Path = DEFAULT_GLOSSARY_PATH,
    do_not_translate_path: str | Path = DEFAULT_DO_NOT_TRANSLATE_PATH,
) -> PolicyIndex:
    """Load and cross-check the existing glossary and protection policy."""

    glossary_file = Path(glossary_path).resolve()
    protection_file = Path(do_not_translate_path).resolve()
    glossary = _load_yaml_mapping(glossary_file)
    protection = _load_yaml_mapping(protection_file)

    glossary_schema = str(glossary.get("schema_version", ""))
    protection_schema = str(protection.get("schema_version", ""))
    if not glossary_schema or glossary_schema != protection_schema:
        raise PolicyConfigurationError(
            "glossary and do-not-translate assets must use the same non-empty schema_version"
        )

    status_values = set(_string_tuple(glossary.get("status_values"), field="status_values"))
    expected_statuses = {status.value for status in TermStatus}
    if status_values != expected_statuses:
        raise PolicyConfigurationError(
            f"status_values must equal {sorted(expected_statuses)}"
        )

    product_candidates: dict[str, tuple[str, ...]] = {}
    governed_terms: list[GovernedTerm] = []
    entries = glossary.get("entries")
    if not isinstance(entries, list):
        raise PolicyConfigurationError("glossary entries must be a list")

    for position, raw_entry in enumerate(entries):
        entry = _mapping(raw_entry, field=f"entries[{position}]")
        term = _mapping(entry.get("term"), field=f"entries[{position}].term")
        display = _mapping(entry.get("display"), field=f"entries[{position}].display")
        term_id = str(entry.get("term_id", ""))
        english = str(term.get("english", ""))
        if not term_id or not english:
            raise PolicyConfigurationError(f"entries[{position}] requires term_id and term.english")
        try:
            status = TermStatus(str(term.get("status", "")))
        except ValueError as exc:
            raise PolicyConfigurationError(
                f"entries[{position}] has an unsupported status"
            ) from exc
        display_mode = str(display.get("mode", ""))
        aliases = _string_tuple(term.get("aliases", []), field=f"entries[{position}].term.aliases")
        chinese_value = term.get("chinese")
        if chinese_value is not None and not isinstance(chinese_value, str):
            raise PolicyConfigurationError(f"entries[{position}].term.chinese must be string or null")
        candidate_values = _string_tuple(
            entry.get("candidate_chinese", []),
            field=f"entries[{position}].candidate_chinese",
        )
        product_candidates[english] = candidate_values
        forbidden_values = tuple(
            dict.fromkeys(
                value
                for value in (*candidate_values, chinese_value, *aliases)
                if isinstance(value, str) and value
            )
        )
        governed_terms.append(
            GovernedTerm(
                term_id=term_id,
                english=english,
                chinese=chinese_value,
                aliases=aliases,
                category=str(term.get("category", "")),
                status=status,
                display_mode=display_mode,
                action=_action_for_display_mode(display_mode),
                forbidden_display_values=forbidden_values,
            )
        )

    exact_terms_section = protection.get("exact_terms", [])
    if not isinstance(exact_terms_section, list):
        raise PolicyConfigurationError("exact_terms must be a list")
    exact_terms: list[str] = []
    for position, item in enumerate(exact_terms_section):
        mapping = _mapping(item, field=f"exact_terms[{position}]")
        value = mapping.get("value")
        if not isinstance(value, str) or not value:
            raise PolicyConfigurationError(f"exact_terms[{position}].value must be non-empty")
        exact_terms.append(value)

    technology = _mapping(protection.get("technology_names"), field="technology_names")
    exact_terms.extend(_string_tuple(technology.get("terms"), field="technology_names.terms"))

    magic = _mapping(
        protection.get("professional_magic_english_only"),
        field="professional_magic_english_only",
    )
    science = _mapping(
        protection.get("cognitive_science_english_only"),
        field="cognitive_science_english_only",
    )
    products = _mapping(protection.get("pending_product_names"), field="pending_product_names")
    magic_english_only = _string_tuple(magic.get("terms"), field="professional_magic_english_only.terms")
    science_english_only = _string_tuple(science.get("terms"), field="cognitive_science_english_only.terms")
    product_names = _string_tuple(products.get("terms"), field="pending_product_names.terms")

    limited_magic = _mapping(
        protection.get("professional_magic_limited_bilingual_candidates"),
        field="professional_magic_limited_bilingual_candidates",
    )
    limited_evidence = _mapping(
        protection.get("limited_bilingual_evidence_and_governance"),
        field="limited_bilingual_evidence_and_governance",
    )
    limited_magic_terms = _mapping(limited_magic.get("terms"), field="professional_magic_limited_bilingual_candidates.terms")
    limited_evidence_terms = _mapping(limited_evidence.get("terms"), field="limited_bilingual_evidence_and_governance.terms")
    limited_terms = tuple(dict.fromkeys((*limited_magic_terms, *limited_evidence_terms)))

    glossary_by_english = {term.english: term for term in governed_terms}
    missing_governed = sorted(
        set((*magic_english_only, *science_english_only, *product_names, *limited_terms))
        - set(glossary_by_english)
    )
    if missing_governed:
        raise PolicyConfigurationError(
            "protected terms missing from glossary: " + ", ".join(missing_governed)
        )

    protected_content = _mapping(
        protection.get("protected_knowledge_content"),
        field="protected_knowledge_content",
    )
    protected_identifiers = _mapping(
        protection.get("protected_identifiers"),
        field="protected_identifiers",
    )
    guards = _mapping(protection.get("current_phase_guards"), field="current_phase_guards")

    return PolicyIndex(
        schema_version=glossary_schema,
        glossary_path=str(glossary_file),
        do_not_translate_path=str(protection_file),
        terms=tuple(governed_terms),
        exact_terms=tuple(dict.fromkeys(exact_terms)),
        product_names=product_names,
        english_only_terms=tuple(dict.fromkeys((*magic_english_only, *science_english_only))),
        limited_bilingual_terms=limited_terms,
        protected_knowledge_fields=_string_tuple(
            protected_content.get("fields"),
            field="protected_knowledge_content.fields",
        ),
        protected_identifier_values=_string_tuple(
            protected_identifiers.get("values"),
            field="protected_identifiers.values",
        ),
        forbidden_target_phrases=FORBIDDEN_TARGET_PHRASES,
        blocked_failures=_string_tuple(protection.get("blocked_failures"), field="blocked_failures"),
        machine_may_set_approved=bool(guards.get("machine_may_set_approved", False)),
    )
