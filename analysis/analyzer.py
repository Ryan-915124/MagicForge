"""Evidence-bound, rubric-driven Magic Theory Analyzer."""

from __future__ import annotations

import json
import re
from pathlib import Path

from analysis.models import (
    AnalysisDimension,
    AnalysisDimensionDraft,
    AnalyzerDraft,
    MagicTheoryAnalysis,
)
from llm.glm_client import GLMClient
from retrieval.interfaces import SearchResult


PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "analyzer_prompt.txt"


class AnalysisFrameworkError(RuntimeError):
    pass


class MagicTheoryAnalyzer:
    """Require a complete domain rubric, then calculate scores deterministically."""

    RUBRIC = {
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

    def __init__(self, llm: GLMClient) -> None:
        self.llm = llm
        try:
            self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            raise AnalysisFrameworkError(f"could not load analyzer prompt: {exc}") from exc

    def analyze(
        self, description: str, sources: list[SearchResult]
    ) -> MagicTheoryAnalysis:
        context = self._format_context(sources)
        prompt = (
            f"Performance description:\n{description}\n\n"
            f"Retrieved knowledge:\n{context}\n\n"
            "Required rubric (criterion names must match exactly):\n"
            f"{json.dumps(self.RUBRIC, ensure_ascii=False)}\n\n"
            "Return JSON matching this schema exactly:\n"
            f"{json.dumps(AnalyzerDraft.model_json_schema(), ensure_ascii=False)}"
        )
        draft = self.llm.generate_structured(
            prompt,
            AnalyzerDraft,
            system_prompt=self.system_prompt,
            temperature=0.2,
            max_tokens=4_000,
        )
        return self._finalize(draft, len(sources))

    def _finalize(self, draft: AnalyzerDraft, source_count: int) -> MagicTheoryAnalysis:
        dimensions: dict[str, AnalysisDimension] = {}
        for name, required in self.RUBRIC.items():
            section: AnalysisDimensionDraft = getattr(draft, name)
            by_name = {
                _normalize_criterion(item.criterion): item for item in section.criteria
            }
            missing = [item for item in required if item not in by_name]
            if missing:
                raise AnalysisFrameworkError(
                    f"analysis dimension {name} is missing criteria: {', '.join(missing)}"
                )
            selected = [by_name[item] for item in required]
            for criterion in selected:
                invalid_sources = [
                    number
                    for number in criterion.source_numbers
                    if number < 1 or number > source_count
                ]
                if invalid_sources:
                    raise AnalysisFrameworkError(
                        f"criterion {criterion.criterion} cites unavailable sources: "
                        + ", ".join(str(item) for item in invalid_sources)
                    )
            score = round(sum(item.score for item in selected) / len(selected), 2)
            dimensions[name] = AnalysisDimension(
                score=score,
                summary=section.summary,
                criteria=selected,
                risks=section.risks,
                recommendations=section.recommendations,
            )

        overall_score = round(
            sum(item.score for item in dimensions.values()) / len(dimensions), 2
        )
        return MagicTheoryAnalysis(
            overall_score=overall_score,
            **dimensions,
            assumptions=draft.assumptions,
            overall_assessment=draft.overall_assessment,
        )

    @staticmethod
    def _format_context(sources: list[SearchResult]) -> str:
        if not sources:
            return "No source passages were retrieved. Leave source_numbers empty."
        sections = []
        for index, source in enumerate(sources, start=1):
            title = source.payload.get("title") or "Untitled source"
            author = source.payload.get("author") or "Unknown author"
            locator = source.payload.get("source_locator") or "unknown location"
            evidence = _format_evidence_metadata(source)
            sections.append(
                f"[Source {index}: {title} - {author}, {locator}; {evidence}]\n"
                f"{source.text}"
            )
        return "\n\n".join(sections)


def _normalize_criterion(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _format_evidence_metadata(source: SearchResult) -> str:
    payload = source.payload
    metadata = [
        f"knowledge_type={payload.get('knowledge_type') or 'unknown'}",
        f"origin={payload.get('knowledge_origin') or 'unknown'}",
        f"evidence_level={payload.get('evidence_level') or 'unknown'}",
        f"confidence={payload.get('confidence_label') or 'unknown'}",
        f"contradiction={payload.get('contradiction_status') or 'unknown'}",
    ]
    limitations = payload.get("limitations") or []
    if isinstance(limitations, list) and limitations:
        metadata.append(
            "limitations=" + "; ".join(str(item) for item in limitations)
        )
    return "; ".join(metadata)
