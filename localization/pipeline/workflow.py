"""Deterministic catalog classification and baseline auditing."""

from __future__ import annotations

from collections import Counter, defaultdict
import re
from typing import Any, Iterable

from localization.pipeline.models import (
    GenerationOrigin,
    LocalizationAction,
    LocalizationClassification,
    PolicyIndex,
    ProposalStatus,
    SourceUnit,
    TermStatus,
    TranslationProposal,
    ValidationReport,
    compute_source_hash,
    extract_placeholders,
)
from localization.pipeline.validator import LocalizationValidator


def build_source_units(
    inventory: dict[str, Any],
    policy: PolicyIndex,
) -> list[SourceUnit]:
    """Build governed source units from the AST inventory's canonical catalog."""

    catalogs = inventory["catalogs"]
    source_catalog: dict[str, str] = catalogs["enUS"]
    target_catalog: dict[str, str] = catalogs["zhCN"]
    contexts: dict[str, list[str]] = defaultdict(list)
    for call in inventory.get("usage", {}).get("calls", []):
        key = call.get("key")
        if key in source_catalog:
            contexts[key].append(
                f"{call.get('file', 'unknown')}:{call.get('line', 0)}:{call.get('column', 0)}"
            )

    validator = LocalizationValidator(policy)
    units: list[SourceUnit] = []
    for key, source_text in source_catalog.items():
        protected_terms = validator.detect_protected_terms(source_text)
        classification, action = classify_unit(source_text, protected_terms, policy)
        units.append(
            SourceUnit(
                key=key,
                source_text=source_text,
                source_hash=compute_source_hash(source_text),
                current_target=target_catalog.get(key),
                classification=classification,
                action=action,
                placeholders=extract_placeholders(source_text),
                protected_terms=protected_terms,
                context=tuple(contexts.get(key, ()))[:12],
            )
        )
    return units


def govern_hardcoded_candidates(
    inventory: dict[str, Any],
    policy: PolicyIndex,
) -> list[dict[str, Any]]:
    """Add non-authoritative governance hints to AST hardcoded findings."""

    validator = LocalizationValidator(policy)
    output: list[dict[str, Any]] = []
    for finding in inventory.get("hardcodedCandidates", []):
        value = str(finding.get("value", ""))
        protected_terms = validator.detect_protected_terms(value)
        if re.fullmatch(r"[A-Z0-9 /._♠-]+", value):
            classification = LocalizationClassification.TECHNICAL_IDENTIFIER
            action = LocalizationAction.PRESERVE_EXACT
        else:
            classification, action = classify_unit(value, protected_terms, policy)
        output.append(
            {
                **finding,
                "classification": classification.value,
                "action": action.value,
                "protected_terms": list(protected_terms),
                "disposition": (
                    "preserve_in_source"
                    if action in {
                        LocalizationAction.PRESERVE_EXACT,
                        LocalizationAction.PRESERVE_ENGLISH_ONLY,
                    }
                    else "catalog_candidate"
                ),
                "automatic_rewrite": False,
            }
        )
    return output


def classify_unit(
    source_text: str,
    protected_terms: tuple[str, ...],
    policy: PolicyIndex,
) -> tuple[LocalizationClassification, LocalizationAction]:
    """Classify with exact context before any substring-based mixed handling."""

    exact_source = source_text.strip()
    governed = policy.terms_by_english.get(exact_source)
    if governed is not None:
        classification = _classification_for_category(governed.category)
        if governed.display_mode == "localized_ui":
            return classification, LocalizationAction.NORMAL_UI_TRANSLATION
        if governed.display_mode == "source_original":
            return classification, LocalizationAction.PRESERVE_EXACT
        if governed.display_mode == "limited_bilingual":
            if governed.status == TermStatus.APPROVED:
                return classification, LocalizationAction.LIMITED_BILINGUAL_SUPPORT
            return classification, LocalizationAction.PRESERVE_ENGLISH_ONLY
        return classification, LocalizationAction.PRESERVE_ENGLISH_ONLY

    if exact_source in policy.exact_terms:
        return (
            LocalizationClassification.TECHNICAL_IDENTIFIER,
            LocalizationAction.PRESERVE_EXACT,
        )
    if exact_source in policy.product_names:
        return (
            LocalizationClassification.PRODUCT_NAMING,
            LocalizationAction.PRESERVE_ENGLISH_ONLY,
        )
    if exact_source in policy.english_only_terms:
        return _classification_for_english_only(exact_source, policy), LocalizationAction.PRESERVE_ENGLISH_ONLY
    if exact_source in policy.limited_bilingual_terms:
        # Every current limited-bilingual term is draft. Fail closed until its
        # glossary status is explicitly approved.
        return _classification_for_english_only(exact_source, policy), LocalizationAction.PRESERVE_ENGLISH_ONLY
    if protected_terms:
        return LocalizationClassification.MIXED, LocalizationAction.NORMAL_UI_TRANSLATION
    return LocalizationClassification.UI_TRANSLATION, LocalizationAction.NORMAL_UI_TRANSLATION


def audit_existing_targets(
    units: Iterable[SourceUnit],
    validator: LocalizationValidator,
) -> tuple[list[TranslationProposal], ValidationReport]:
    """Validate the current zh-CN catalog as a deterministic baseline."""

    source_units = list(units)
    proposals = [
        TranslationProposal(
            key=unit.key,
            source_text=unit.source_text,
            source_hash=unit.source_hash,
            candidate_chinese=unit.current_target or unit.source_text,
            rationale="Deterministic audit of the current runtime target.",
            confidence=1.0,
            status=ProposalStatus.MACHINE_REVIEWED,
            provider="deterministic",
            protected_terms=unit.protected_terms,
            generated_by=GenerationOrigin.DETERMINISTIC,
        )
        for unit in source_units
    ]
    return proposals, validator.validate_batch(source_units, proposals)


def select_units(
    units: Iterable[SourceUnit],
    *,
    keys: Iterable[str] = (),
    modules: Iterable[str] = (),
    all_normal_ui: bool = False,
) -> list[SourceUnit]:
    source_units = list(units)
    requested_keys = set(keys)
    requested_modules = tuple(f"{module.rstrip('.')}." for module in modules)
    known_keys = {unit.key for unit in source_units}
    unknown = sorted(requested_keys - known_keys)
    if unknown:
        raise ValueError("unknown localization key(s): " + ", ".join(unknown))

    selected: list[SourceUnit] = []
    for unit in source_units:
        if unit.key in requested_keys:
            selected.append(unit)
            continue
        if requested_modules and unit.key.startswith(requested_modules):
            selected.append(unit)
            continue
        if all_normal_ui and unit.action == LocalizationAction.NORMAL_UI_TRANSLATION:
            selected.append(unit)
    return selected


def render_summary(
    *,
    title: str,
    inventory: dict[str, Any],
    units: list[SourceUnit],
    report: ValidationReport,
    proposal_count: int,
    llm_invoked: bool,
) -> str:
    classifications = Counter(unit.classification.value for unit in units)
    actions = Counter(unit.action.value for unit in units)
    lines = [
        f"# {title}",
        "",
        "## Result",
        "",
        f"- Status: {'PASS' if report.valid else 'FAIL'}",
        f"- Source units: {len(units)}",
        f"- Proposals: {proposal_count}",
        f"- GLM invoked: {'yes' if llm_invoked else 'no'}",
        f"- Validation errors: {report.error_count}",
        f"- Validation warnings: {report.warning_count}",
        f"- Hardcoded UI candidates: {inventory['summary']['hardcodedCandidates']}",
        "",
        "## Classification",
        "",
    ]
    lines.extend(f"- `{name}`: {count}" for name, count in sorted(classifications.items()))
    lines.extend(["", "## Actions", ""])
    lines.extend(f"- `{name}`: {count}" for name, count in sorted(actions.items()))
    lines.extend(["", "## Validation findings", ""])
    if not report.issues:
        lines.append("No deterministic policy violations were found.")
    else:
        for issue in report.issues[:100]:
            location = f" `{issue.key}`" if issue.key else ""
            lines.append(f"- **{issue.severity.value.upper()}** `{issue.code}`{location}: {issue.message}")
        if len(report.issues) > 100:
            lines.append(f"- … {len(report.issues) - 100} additional findings are in `validation-report.json`.")
    lines.extend(
        [
            "",
            "## Governance boundary",
            "",
            "Machine output is a draft candidate. This run did not change the English catalog, runtime messages, glossary approvals, or Translation Memory.",
        ]
    )
    return "\n".join(lines) + "\n"


def _classification_for_category(category: str) -> LocalizationClassification:
    if category == "product_name":
        return LocalizationClassification.PRODUCT_NAMING
    if category.startswith("magic_"):
        return LocalizationClassification.MAGIC_TERMINOLOGY
    if category == "cognitive_science":
        return LocalizationClassification.ACADEMIC_TERMINOLOGY
    if category in {"evidence_model", "evidence_governance", "governance", "knowledge_model"}:
        return LocalizationClassification.EVIDENCE_GOVERNANCE
    if category == "citation":
        return LocalizationClassification.SOURCE_CITATION
    if category == "technology":
        return LocalizationClassification.TECHNICAL_IDENTIFIER
    return LocalizationClassification.UI_TRANSLATION


def _classification_for_english_only(
    term: str,
    policy: PolicyIndex,
) -> LocalizationClassification:
    governed = policy.terms_by_english.get(term)
    return (
        _classification_for_category(governed.category)
        if governed is not None
        else LocalizationClassification.UNCLASSIFIED
    )
