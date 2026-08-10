"""GLM-only candidate proposer for the local localization workflow.

This module performs no catalog, runtime, or Translation Memory writes.  It
sends bounded UI-only batches to the existing GLM adapter and rejects any
response that changes canonical input identity.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from localization.pipeline.models import (
    GenerationOrigin,
    ProposalStatus,
    SourceUnit,
    TranslationProposal,
    extract_placeholders,
)
from localization.pipeline.policy import load_policy_index
from localization.pipeline.validator import LocalizationValidator


PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "ui-localizer-v1.md"
DEFAULT_BATCH_SIZE = 8
MAX_BATCH_SIZE = 16
DEFAULT_BATCH_CHARACTER_BUDGET = 12_000


class LocalizationProposalError(RuntimeError):
    """Raised when a proposal batch violates the localization contract."""


class StructuredGLMClient(Protocol):
    """The injectable subset of :class:`llm.glm_client.GLMClient` used here."""

    def generate_structured(
        self,
        prompt: str,
        response_model: type[BaseModel],
        **kwargs: Any,
    ) -> Any: ...


class _GLMProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    candidate_chinese: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    # This value is fixed by the trusted local caller. GLM may omit it, but it
    # can never choose another status or promote a proposal.
    status: Literal["machine_proposed"] = "machine_proposed"
    rationale: str = Field(min_length=1, max_length=320)


class _GLMBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[_GLMProposal] = Field(min_length=1, max_length=MAX_BATCH_SIZE)


class GLMLocalizationProposer:
    """Generate unapproved Chinese UI candidates through the existing GLM client.

    ``llm`` may be a real ``GLMClient`` or a test fake implementing
    ``generate_structured``.  Calls are deliberately synchronous because the
    existing adapter is synchronous and this class is intended for a local CLI.
    """

    def __init__(
        self,
        llm: StructuredGLMClient,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_character_budget: int = DEFAULT_BATCH_CHARACTER_BUDGET,
        prompt_path: Path = PROMPT_PATH,
        validator: LocalizationValidator | None = None,
    ) -> None:
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
        if batch_character_budget < 1_000:
            raise ValueError("batch_character_budget must be at least 1000")
        self.llm = llm
        self.batch_size = batch_size
        self.batch_character_budget = batch_character_budget
        self.validator = validator or LocalizationValidator(load_policy_index())
        try:
            self.system_prompt = prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LocalizationProposalError(
                f"could not load localization prompt {prompt_path}: {exc}"
            ) from exc

    def propose(self, units: Iterable[SourceUnit]) -> list[TranslationProposal]:
        """Return machine proposals without writing them to any destination."""

        source_units = list(units)
        if not source_units:
            return []
        self._validate_input_identity(source_units)

        proposals: list[TranslationProposal] = []
        for batch in self._partition(source_units):
            generated = self._generate_batch(batch)
            proposals.extend(self._validate_and_convert(batch, generated))
        return proposals

    def _generate_batch(self, units: list[SourceUnit]) -> _GLMBatch:
        payload = {
            "source_locale": "en-US",
            "target_locale": "zh-CN",
            "units": [self._unit_payload(unit) for unit in units],
        }
        prompt = (
            "Propose zh-CN UI localization candidates for this bounded batch.\n"
            "The JSON schema is:\n"
            f"{json.dumps(_GLMBatch.model_json_schema(), ensure_ascii=False)}\n\n"
            "BATCH_INPUT:\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        )
        response = self.llm.generate_structured(
            prompt,
            _GLMBatch,
            system_prompt=self.system_prompt,
            temperature=0.0,
            max_tokens=4_000,
            thinking_enabled=False,
        )
        if isinstance(response, _GLMBatch):
            return response
        try:
            return _GLMBatch.model_validate(response)
        except Exception as exc:
            raise LocalizationProposalError(
                "GLM returned a response outside the localization batch schema"
            ) from exc

    def _unit_payload(self, unit: SourceUnit) -> dict[str, Any]:
        """Serialize only the explicitly allowed UI fields, never arbitrary data."""

        return {
            "key": unit.key,
            "source_text": unit.source_text,
            "localization_source": self.validator.canonicalize_source_terms(
                unit.source_text
            ),
            "source_hash": unit.source_hash,
            "current_target": unit.current_target,
            "classification": _plain_value(unit.classification),
            "action": _plain_value(unit.action),
            "placeholders": list(unit.placeholders),
            "protected_terms": list(unit.protected_terms),
            "required_target_terms": [
                {
                    "canonical": term,
                    "minimum_occurrences": self.validator.source_term_occurrence_count(
                        unit.source_text,
                        term,
                    ),
                }
                for term in unit.protected_terms
            ],
            "context": list(unit.context),
        }

    def _partition(self, units: list[SourceUnit]) -> list[list[SourceUnit]]:
        batches: list[list[SourceUnit]] = []
        current: list[SourceUnit] = []
        current_characters = 0
        for unit in units:
            size = len(
                json.dumps(self._unit_payload(unit), ensure_ascii=False, separators=(",", ":"))
            )
            if size > self.batch_character_budget:
                raise LocalizationProposalError(
                    f"UI unit {unit.key!r} exceeds the localization batch character budget"
                )
            if current and (
                len(current) >= self.batch_size
                or current_characters + size > self.batch_character_budget
            ):
                batches.append(current)
                current = []
                current_characters = 0
            current.append(unit)
            current_characters += size
        if current:
            batches.append(current)
        return batches

    def _validate_input_identity(self, units: list[SourceUnit]) -> None:
        keys = [unit.key for unit in units]
        duplicate_keys = sorted(key for key, count in Counter(keys).items() if count > 1)
        if duplicate_keys:
            raise LocalizationProposalError(
                "duplicate source keys are not allowed: " + ", ".join(duplicate_keys)
            )
        for unit in units:
            issues = self.validator.validate_unit(unit)
            if issues:
                raise LocalizationProposalError(
                    f"source unit {unit.key!r} is stale or violates localization policy: "
                    f"{issues[0].code}"
                )

    def _validate_and_convert(
        self, units: list[SourceUnit], response: _GLMBatch
    ) -> list[TranslationProposal]:
        expected_keys = [unit.key for unit in units]
        actual_keys = [proposal.key for proposal in response.proposals]
        if actual_keys != expected_keys:
            raise LocalizationProposalError(
                "GLM proposal keys must match the input keys exactly and in input order"
            )

        output: list[TranslationProposal] = []
        for unit, proposal in zip(units, response.proposals, strict=True):
            if proposal.status != "machine_proposed":
                raise LocalizationProposalError(
                    f"GLM returned an invalid proposal status for {unit.key!r}"
                )
            _validate_target_preservation(
                unit,
                proposal.candidate_chinese,
                self.validator,
            )
            output.append(_build_translation_proposal(unit, proposal))
        return output


def _build_translation_proposal(
    unit: SourceUnit, proposal: _GLMProposal
) -> TranslationProposal:
    """Combine trusted source metadata with the validated candidate text."""

    return TranslationProposal(
        key=unit.key,
        source_text=unit.source_text,
        source_hash=unit.source_hash,
        candidate_chinese=proposal.candidate_chinese,
        rationale=proposal.rationale,
        confidence=proposal.confidence,
        status=ProposalStatus.MACHINE_PROPOSED,
        provider="glm",
        protected_terms=unit.protected_terms,
        generated_by=GenerationOrigin.LOCAL_AI,
        source_locale=unit.source_locale,
        target_locale=unit.target_locale,
    )


def _validate_target_preservation(
    unit: SourceUnit,
    target_text: str,
    validator: LocalizationValidator,
) -> None:
    action = str(_plain_value(unit.action)).casefold()
    if action in {
        "preserve_exact",
        "preserve_english_only",
        "english_only",
        "product_name_review",
        "source_original",
        "skip",
    } and target_text != unit.source_text:
        raise LocalizationProposalError(
            f"proposal for {unit.key!r} violates the required preservation action"
        )

    if Counter(extract_placeholders(target_text)) != Counter(unit.placeholders):
        raise LocalizationProposalError(
            f"proposal for {unit.key!r} changed one or more placeholders"
        )
    for term in unit.protected_terms:
        expected_count = validator.source_term_occurrence_count(unit.source_text, term)
        actual_count = validator.target_canonical_occurrence_count(target_text, term)
        if expected_count and actual_count < expected_count:
            raise LocalizationProposalError(
                f"proposal for {unit.key!r} changed protected term {term!r}"
            )


def _plain_value(value: Any) -> Any:
    return getattr(value, "value", value)
