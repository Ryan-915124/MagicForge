"""Deterministic validation gates for local Chinese localization proposals."""

from __future__ import annotations

from collections import Counter
import re

from localization.pipeline.models import (
    BatchValidationRequest,
    GovernedTerm,
    LocalizationAction,
    LocalizationClassification,
    PolicyIndex,
    SourceUnit,
    TermStatus,
    TranslationProposal,
    ValidationIssue,
    ValidationReport,
    ValidationSeverity,
    compute_source_hash,
    extract_placeholders,
)


_SOURCE_VARIANT_CATEGORIES = frozenset(
    {
        "cognitive_science",
        "evidence_model",
        "evidence_governance",
        "governance",
        "knowledge_model",
        "citation",
        "magic_ontology",
    }
)
_NAMED_PLACEHOLDER_PATTERN = re.compile(r"\{[A-Za-z][A-Za-z0-9_]*\}")


def _mask_named_placeholders(text: str) -> str:
    """Hide interpolation identifiers from terminology matching."""

    return _NAMED_PLACEHOLDER_PATTERN.sub(
        lambda match: " " * (match.end() - match.start()),
        text,
    )


def _bounded_pattern(expression: str, term: str, *, flags: int = 0) -> re.Pattern[str]:
    prefix = r"(?<![A-Za-z0-9_])" if term[0].isalnum() else ""
    suffix = r"(?![A-Za-z0-9_])" if term[-1].isalnum() else ""
    return re.compile(prefix + expression + suffix, flags)


def _canonical_term_pattern(term: str) -> re.Pattern[str]:
    """Match the exact canonical English spelling used in a target string."""

    return _bounded_pattern(re.escape(term), term)


def _simple_plural(term: str) -> str | None:
    """Return a deliberately small plural variant for a canonical phrase.

    Only the final ASCII word is inflected.  This is intentionally not a
    general English morphology engine: the policy uses it to recognize common
    UI forms such as ``claims``, ``sources``, and ``cognitive mechanisms``.
    """

    head, separator, final = term.rpartition(" ")
    lowered = final.casefold()
    if lowered.endswith("ss") or lowered.endswith(("ch", "sh", "x", "z")):
        plural_final = final + "es"
    elif lowered.endswith("s"):
        # The canonical term is already plural (for example, Limitations).
        return None
    elif len(final) > 1 and lowered.endswith("y") and lowered[-2] not in "aeiou":
        plural_final = final[:-1] + "ies"
    else:
        plural_final = final + "s"
    return f"{head}{separator}{plural_final}"


def _source_term_pattern(term: str, governed: GovernedTerm | None) -> re.Pattern[str]:
    """Match canonical source terms using category-scoped source variants.

    Product names, technologies, and professional magic terms remain strict,
    case-sensitive matches.  Only cognitive-science and evidence/governance
    vocabulary accepts source-side casing and simple plural variation.  This
    prevents ordinary UI verbs such as ``load`` or ``force`` from being
    mistaken for the professional terms ``Load`` and ``Force``.
    """

    if governed is None or governed.category not in _SOURCE_VARIANT_CATEGORIES:
        return _canonical_term_pattern(term)
    variants = [term]
    plural = _simple_plural(term)
    if plural is not None:
        variants.append(plural)
    expression = "(?:" + "|".join(re.escape(value) for value in variants) + ")"
    return _bounded_pattern(expression, term, flags=re.IGNORECASE)


class LocalizationValidator:
    """Validate proposals without mutating source messages or governance assets."""

    def __init__(self, policy: PolicyIndex) -> None:
        self.policy = policy
        self._governed_by_english = policy.terms_by_english

    def detect_protected_terms(self, source_text: str) -> tuple[str, ...]:
        """Find non-overlapping governed terms, preferring the longest match."""

        return tuple(
            dict.fromkeys(term for _, _, term in self._source_term_matches(source_text))
        )

    def canonicalize_source_terms(self, source_text: str) -> str:
        """Normalize governed source variants without changing other source text."""

        normalized = source_text
        for start, end, canonical in reversed(self._source_term_matches(source_text)):
            normalized = normalized[:start] + canonical + normalized[end:]
        return normalized

    def _source_term_matches(self, source_text: str) -> list[tuple[int, int, str]]:
        """Return non-overlapping source spans mapped to canonical policy terms."""

        candidates = tuple(
            dict.fromkeys(
                (
                    *self.policy.product_names,
                    *self.policy.exact_terms,
                    *self.policy.english_only_terms,
                    *self.policy.limited_bilingual_terms,
                )
            )
        )
        searchable_source = _mask_named_placeholders(source_text)
        matches: list[tuple[int, int, str]] = []
        for term in candidates:
            governed = self._governed_by_english.get(term)
            matches.extend(
                (match.start(), match.end(), term)
                for match in _source_term_pattern(term, governed).finditer(searchable_source)
            )
        matches.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))

        selected: list[tuple[int, int, str]] = []
        for start, end, term in matches:
            if any(start < selected_end and end > selected_start for selected_start, selected_end, _ in selected):
                continue
            selected.append((start, end, term))
        return selected

    def source_term_occurrence_count(self, source_text: str, canonical: str) -> int:
        """Count policy-supported source variants for one canonical term."""

        governed = self._governed_by_english.get(canonical)
        searchable_source = _mask_named_placeholders(source_text)
        return len(_source_term_pattern(canonical, governed).findall(searchable_source))

    @staticmethod
    def target_canonical_occurrence_count(target_text: str, canonical: str) -> int:
        """Count only byte-cased canonical English occurrences in a target."""

        return len(_canonical_term_pattern(canonical).findall(target_text))

    def validate_unit(self, unit: SourceUnit) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        expected_hash = compute_source_hash(unit.source_text)
        if unit.source_hash != expected_hash:
            issues.append(
                self._error(
                    "source_hash_mismatch",
                    unit.key,
                    "source_hash does not match the canonical English source text",
                    expected=expected_hash,
                    actual=unit.source_hash,
                )
            )

        expected_placeholders = extract_placeholders(unit.source_text)
        if Counter(unit.placeholders) != Counter(expected_placeholders):
            issues.append(
                self._error(
                    "declared_placeholders_mismatch",
                    unit.key,
                    "declared placeholders do not match source text",
                    expected=list(expected_placeholders),
                    actual=list(unit.placeholders),
                )
            )

        detected_terms = self.detect_protected_terms(unit.source_text)
        if set(unit.protected_terms) != set(detected_terms):
            issues.append(
                self._error(
                    "declared_protected_terms_mismatch",
                    unit.key,
                    "declared protected terms do not match policy detection",
                    expected=list(detected_terms),
                    actual=list(unit.protected_terms),
                )
            )

        return issues

    def validate_proposal(
        self,
        unit: SourceUnit,
        proposal: TranslationProposal,
    ) -> list[ValidationIssue]:
        issues = self.validate_unit(unit)
        if proposal.key != unit.key:
            issues.append(
                self._error(
                    "proposal_key_mismatch",
                    unit.key,
                    "proposal key does not match source unit key",
                    proposal_key=proposal.key,
                )
            )
        if proposal.source_text != unit.source_text:
            issues.append(
                self._error(
                    "proposal_source_text_mismatch",
                    unit.key,
                    "proposal source text differs from the canonical source unit",
                )
            )
        if proposal.source_hash != unit.source_hash:
            issues.append(
                self._error(
                    "proposal_source_hash_mismatch",
                    unit.key,
                    "proposal source hash differs from the source unit",
                    expected=unit.source_hash,
                    actual=proposal.source_hash,
                )
            )

        target_placeholders = extract_placeholders(proposal.candidate_chinese)
        if Counter(target_placeholders) != Counter(unit.placeholders):
            issues.append(
                self._error(
                    "target_placeholders_mismatch",
                    unit.key,
                    "candidate placeholders must preserve source names and multiplicity",
                    expected=list(unit.placeholders),
                    actual=list(target_placeholders),
                )
            )

        detected_terms = self.detect_protected_terms(unit.source_text)
        if set(proposal.protected_terms) != set(detected_terms):
            issues.append(
                self._error(
                    "proposal_protected_terms_mismatch",
                    unit.key,
                    "proposal protected terms do not match the policy index",
                    expected=list(detected_terms),
                    actual=list(proposal.protected_terms),
                )
            )

        if proposal.provider not in self.policy.allowed_machine_providers:
            issues.append(
                self._error(
                    "unapproved_machine_provider",
                    unit.key,
                    "proposal provider is outside the local GLM/tool allowlist",
                    provider=proposal.provider,
                    allowed=list(self.policy.allowed_machine_providers),
                )
            )

        for phrase in self.policy.forbidden_target_phrases:
            if phrase in proposal.candidate_chinese:
                issues.append(
                    self._error(
                        "forbidden_target_phrase",
                        unit.key,
                        "candidate contains wording forbidden by MagicForge brand governance",
                        phrase=phrase,
                    )
                )

        exact_required = unit.action in {
            LocalizationAction.PRESERVE_EXACT,
            LocalizationAction.PRESERVE_ENGLISH_ONLY,
            LocalizationAction.SKIP,
        } or unit.classification in {
            LocalizationClassification.SOURCE_CITATION,
            LocalizationClassification.KNOWLEDGE_CONTENT,
            LocalizationClassification.TECHNICAL_IDENTIFIER,
        }
        if exact_required and proposal.candidate_chinese != unit.source_text:
            issues.append(
                self._error(
                    "preserve_exact_violation",
                    unit.key,
                    "protected source or identifier content must remain byte-for-byte unchanged",
                )
            )

        for term in detected_terms:
            governed = self._governed_by_english.get(term)
            required_count = self.source_term_occurrence_count(unit.source_text, term)
            actual_count = self.target_canonical_occurrence_count(
                proposal.candidate_chinese,
                term,
            )
            if term in self.policy.product_names:
                issues.extend(
                    self._validate_preserved_term(
                        unit.key,
                        term,
                        required_count,
                        actual_count,
                        "product_name_not_preserved",
                    )
                )
                issues.extend(self._validate_forbidden_display_values(unit.key, proposal, governed, term))
                continue
            if term in self.policy.exact_terms:
                issues.extend(
                    self._validate_preserved_term(
                        unit.key,
                        term,
                        required_count,
                        actual_count,
                        "exact_term_not_preserved",
                    )
                )
                continue
            if term in self.policy.english_only_terms:
                issues.extend(
                    self._validate_preserved_term(
                        unit.key,
                        term,
                        required_count,
                        actual_count,
                        "english_only_term_not_preserved",
                    )
                )
                issues.extend(self._validate_forbidden_display_values(unit.key, proposal, governed, term))
                continue
            if term in self.policy.limited_bilingual_terms:
                issues.extend(
                    self._validate_preserved_term(
                        unit.key,
                        term,
                        required_count,
                        actual_count,
                        "limited_bilingual_english_not_preserved",
                    )
                )
                if governed and governed.status != TermStatus.APPROVED:
                    issues.extend(self._validate_forbidden_display_values(unit.key, proposal, governed, term))

        return self._deduplicate(issues)

    def validate_batch(
        self,
        source_units: tuple[SourceUnit, ...] | list[SourceUnit],
        proposals: tuple[TranslationProposal, ...] | list[TranslationProposal],
    ) -> ValidationReport:
        units = tuple(source_units)
        candidate_proposals = tuple(proposals)
        issues: list[ValidationIssue] = []
        unit_counts = Counter(unit.key for unit in units)
        proposal_counts = Counter(proposal.key for proposal in candidate_proposals)

        for key, count in sorted(unit_counts.items()):
            if count > 1:
                issues.append(
                    self._error(
                        "duplicate_source_key",
                        key,
                        "source batch contains a duplicate message key",
                        count=count,
                    )
                )
        for key, count in sorted(proposal_counts.items()):
            if count > 1:
                issues.append(
                    self._error(
                        "duplicate_proposal_key",
                        key,
                        "proposal batch contains a duplicate message key",
                        count=count,
                    )
                )

        unit_keys = set(unit_counts)
        proposal_keys = set(proposal_counts)
        for key in sorted(unit_keys - proposal_keys):
            issues.append(
                self._error(
                    "missing_proposal_key",
                    key,
                    "source unit has no proposal in this batch",
                )
            )
        for key in sorted(proposal_keys - unit_keys):
            issues.append(
                self._error(
                    "unknown_proposal_key",
                    key,
                    "proposal key does not exist in the source batch",
                )
            )

        units_by_key = {unit.key: unit for unit in units}
        proposals_by_key = {proposal.key: proposal for proposal in candidate_proposals}
        for key in sorted(unit_keys & proposal_keys):
            issues.extend(self.validate_proposal(units_by_key[key], proposals_by_key[key]))

        return ValidationReport.from_issues(
            self._deduplicate(issues),
            checked_units=len(units),
            checked_proposals=len(candidate_proposals),
        )

    def validate_request(self, request: BatchValidationRequest) -> ValidationReport:
        return self.validate_batch(request.source_units, request.proposals)

    def _validate_preserved_term(
        self,
        key: str,
        term: str,
        expected: int,
        actual: int,
        code: str,
    ) -> list[ValidationIssue]:
        if actual >= expected:
            return []
        return [
            self._error(
                code,
                key,
                "candidate did not preserve every protected English occurrence",
                term=term,
                expected_occurrences=expected,
                actual_occurrences=actual,
            )
        ]

    def _validate_forbidden_display_values(
        self,
        key: str,
        proposal: TranslationProposal,
        governed: GovernedTerm | None,
        canonical: str,
    ) -> list[ValidationIssue]:
        if governed is None:
            return []
        issues: list[ValidationIssue] = []
        for value in governed.forbidden_display_values:
            if value and value in proposal.candidate_chinese:
                issues.append(
                    self._error(
                        "unapproved_term_display",
                        key,
                        "candidate renders an alias or unapproved Chinese support label",
                        canonical=canonical,
                        displayed=value,
                        status=governed.status.value,
                        display_mode=governed.display_mode,
                    )
                )
        return issues

    @staticmethod
    def _error(code: str, key: str | None, message: str, **details: object) -> ValidationIssue:
        return ValidationIssue(
            severity=ValidationSeverity.ERROR,
            code=code,
            key=key,
            message=message,
            details=dict(details),
        )

    @staticmethod
    def _deduplicate(issues: list[ValidationIssue]) -> list[ValidationIssue]:
        seen: set[tuple[str, str | None, str]] = set()
        result: list[ValidationIssue] = []
        for issue in issues:
            identity = (issue.code, issue.key, repr(sorted(issue.details.items())))
            if identity in seen:
                continue
            seen.add(identity)
            result.append(issue)
        return result
