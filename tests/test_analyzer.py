import pytest

from analysis.analyzer import AnalysisFrameworkError, MagicTheoryAnalyzer
from analysis.models import AnalysisDimensionDraft, AnalyzerDraft, CriterionAssessment
from retrieval.interfaces import SearchResult


def _criterion(name: str, score: int, source_numbers=None) -> CriterionAssessment:
    return CriterionAssessment(
        criterion=name,
        score=score,
        rationale=f"Assessment for {name}",
        source_numbers=source_numbers or [],
    )


def _draft(analyzer: MagicTheoryAnalyzer, score: int = 4) -> AnalyzerDraft:
    sections = {
        name: AnalysisDimensionDraft(
            summary=f"{name} summary",
            criteria=[_criterion(criterion, score, [1]) for criterion in criteria],
            risks=["One risk"],
            recommendations=["One recommendation"],
        )
        for name, criteria in analyzer.RUBRIC.items()
    }
    return AnalyzerDraft(
        **sections,
        assumptions=["The handling is not specified."],
        overall_assessment="A strong but conditional design.",
    )


class FakeStructuredLLM:
    def __init__(self) -> None:
        self.draft = None
        self.kwargs = None
        self.prompt = None

    def generate_structured(self, prompt, response_model, **kwargs):
        self.prompt = prompt
        self.kwargs = kwargs
        return self.draft


def test_analyzer_enforces_rubric_and_calculates_scores() -> None:
    llm = FakeStructuredLLM()
    analyzer = MagicTheoryAnalyzer(llm)
    llm.draft = _draft(analyzer)
    sources = [
        SearchResult(
            text="A relevant passage.",
            score=0.9,
            payload={
                "title": "Theory",
                "author": "Expert",
                "knowledge_type": "evidence",
                "knowledge_origin": "scientific_evidence",
                "evidence_level": "empirical",
                "confidence_label": "high",
                "contradiction_status": "none_found",
                "limitations": ["Laboratory setting."],
            },
        )
    ]

    analysis = analyzer.analyze("A signed card rises to the top.", sources)

    assert analysis.overall_score == 4.0
    assert analysis.effect_strength.score == 4.0
    assert len(analysis.performance_design.criteria) == 4
    assert llm.kwargs["temperature"] == 0.2
    assert "origin=scientific_evidence" in llm.prompt
    assert "limitations=Laboratory setting." in llm.prompt


def test_analyzer_rejects_incomplete_rubric() -> None:
    llm = FakeStructuredLLM()
    analyzer = MagicTheoryAnalyzer(llm)
    llm.draft = _draft(analyzer)
    llm.draft.effect_strength.criteria.pop()

    with pytest.raises(AnalysisFrameworkError, match="missing criteria"):
        analyzer.analyze("Description", [SearchResult(text="x", score=1, payload={})])


def test_analyzer_rejects_fabricated_source_numbers() -> None:
    llm = FakeStructuredLLM()
    analyzer = MagicTheoryAnalyzer(llm)
    llm.draft = _draft(analyzer)
    llm.draft.audience_experience.criteria[0].source_numbers = [2]

    with pytest.raises(AnalysisFrameworkError, match="unavailable sources"):
        analyzer.analyze("Description", [SearchResult(text="x", score=1, payload={})])
