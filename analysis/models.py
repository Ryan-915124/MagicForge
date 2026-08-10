"""Typed inputs and outputs for the Magic Theory Analyzer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CriterionAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: str = Field(min_length=1)
    score: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1)
    source_numbers: list[int] = Field(default_factory=list)


class AnalysisDimensionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    criteria: list[CriterionAssessment] = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class AnalyzerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect_strength: AnalysisDimensionDraft
    method_concealment: AnalysisDimensionDraft
    psychological_principles: AnalysisDimensionDraft
    audience_experience: AnalysisDimensionDraft
    performance_design: AnalysisDimensionDraft
    assumptions: list[str] = Field(default_factory=list)
    overall_assessment: str = Field(min_length=1)


class AnalysisDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=1.0, le=5.0)
    summary: str
    criteria: list[CriterionAssessment]
    risks: list[str]
    recommendations: list[str]


class MagicTheoryAnalysis(BaseModel):
    """Final analysis; all numeric scores are calculated by framework code."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "0.2"
    overall_score: float = Field(ge=1.0, le=5.0)
    effect_strength: AnalysisDimension
    method_concealment: AnalysisDimension
    psychological_principles: AnalysisDimension
    audience_experience: AnalysisDimension
    performance_design: AnalysisDimension
    assumptions: list[str]
    overall_assessment: str
