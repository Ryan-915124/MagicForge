"""Offline language-model substitute for the self-contained Demo profile."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from analysis.models import AnalysisDimensionDraft, AnalyzerDraft, CriterionAssessment


StructuredModel = TypeVar("StructuredModel", bound=BaseModel)


class DemoLanguageModel:
    """Deterministic generator that never initializes GLM or performs I/O."""

    model = "deterministic-demo"

    def generate(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        temperature: float = 0.5,
        max_tokens: int = 2_000,
        json_mode: bool = False,
        thinking_enabled: bool | None = None,
    ) -> str:
        del prompt, system_prompt, temperature, max_tokens, json_mode, thinking_enabled
        return (
            "This answer is generated offline from MagicForge's synthetic Demo corpus.\n\n"
            "[[MAGICFORGE_ACT:EFFECT]]\n"
            "The fictional Vanishing Token is presented as a clear change from present "
            "to absent after a motivated visible gesture.\n\n"
            "[[MAGICFORGE_ACT:HIDDEN_STRUCTURE]]\n"
            "The Demo corpus exposes no operational secret. It illustrates how timing, "
            "presentation motivation, and an audience-facing gesture can be represented "
            "as bounded knowledge concepts.\n\n"
            "[[MAGICFORGE_ACT:COGNITIVE_MECHANISM]]\n"
            "Selective Attention and Expectation Framing are offered only as low-confidence "
            "interpretive hypotheses, not as verified scientific findings.\n\n"
            "[[MAGICFORGE_SYNTHESIS]]\n"
            "All retrieved items are self-authored, unverified synthetic records. Use this "
            "profile to inspect the workflow and governed Production data for real research "
            "conclusions."
        )

    def generate_structured(
        self,
        prompt: str,
        response_model: type[StructuredModel],
        *,
        system_prompt: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2_000,
        thinking_enabled: bool | None = None,
    ) -> StructuredModel:
        del prompt, system_prompt, temperature, max_tokens, thinking_enabled
        draft = _demo_analyzer_draft()
        return response_model.model_validate(draft.model_dump(mode="json"))


def _demo_analyzer_draft() -> AnalyzerDraft:
    rubric = {
        "effect_strength": (
            "clarity",
            "impossibility_gap",
            "emotional_stakes",
            "progression",
        ),
        "method_concealment": (
            "method_effect_distance",
            "naturalness",
            "layering",
            "vulnerability_control",
        ),
        "psychological_principles": (
            "attention_management",
            "assumption_design",
            "memory_management",
            "choice_architecture",
        ),
        "audience_experience": (
            "initial_comprehension",
            "conviction",
            "emotional_arc",
            "aftermath",
        ),
        "performance_design": (
            "motivation",
            "pacing",
            "staging",
            "practicality",
        ),
    }

    def dimension(label: str, criteria: tuple[str, ...]) -> AnalysisDimensionDraft:
        return AnalysisDimensionDraft(
            summary=f"Offline illustrative assessment for {label.replace('_', ' ')}.",
            criteria=[
                CriterionAssessment(
                    criterion=criterion,
                    score=3,
                    rationale=(
                        "Neutral Demo score; this offline profile does not infer facts "
                        "beyond its synthetic records."
                    ),
                    source_numbers=[],
                )
                for criterion in criteria
            ],
            risks=["Synthetic Demo evidence cannot support a real-world conclusion."],
            recommendations=["Validate the question against governed Production evidence."],
        )

    return AnalyzerDraft(
        **{
            label: dimension(label, criteria)
            for label, criteria in rubric.items()
        },
        assumptions=["Only synthetic Demo records are in scope."],
        overall_assessment=(
            "A deterministic illustration of the analyzer structure, not an expert or "
            "scientific judgment."
        ),
    )


__all__ = ["DemoLanguageModel"]
