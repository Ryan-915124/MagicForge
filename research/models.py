"""Storage-neutral models for reproducible research discovery."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DiscoveryProvider(StrEnum):
    EXA = "exa"
    TAVILY = "tavily"


class SourceCategory(StrEnum):
    ACADEMIC = "academic"
    PRACTITIONER = "practitioner"
    WEB = "web"


class ContentAccess(StrEnum):
    SEARCH_SNIPPET = "search_snippet"
    ABSTRACT = "abstract"
    WEB_EXTRACT = "web_extract"
    FULL_TEXT = "full_text"
    PDF_TEXT = "pdf_text"


class ResearchProtocol(BaseModel):
    """Reproducible scope and screening contract established before discovery."""

    model_config = ConfigDict(extra="forbid")

    research_question: str = Field(min_length=3)
    academic_queries: list[str] = Field(default_factory=list)
    practitioner_queries: list[str] = Field(default_factory=list)
    inclusion_criteria: list[str] = Field(default_factory=list)
    exclusion_criteria: list[str] = Field(default_factory=list)
    date_from: int | None = Field(default=None, ge=1800, le=2200)
    date_to: int | None = Field(default=None, ge=1800, le=2200)
    languages: list[str] = Field(default_factory=lambda: ["en"])
    max_results_per_query: int = Field(default=10, ge=1, le=100)

    @model_validator(mode="after")
    def validate_dates(self) -> "ResearchProtocol":
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be later than date_to")
        if not self.academic_queries:
            self.academic_queries = [self.research_question]
        if not self.practitioner_queries:
            self.practitioner_queries = [self.research_question]
        return self


class SearchProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: DiscoveryProvider
    tool_name: str
    query: str
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provider_result_id: str | None = None
    rank: int | None = Field(default=None, ge=1)


class ResearchCandidate(BaseModel):
    """Unverified external source candidate; never directly ingest this model."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    authors: list[str] = Field(default_factory=list)
    published_year: int | None = Field(default=None, ge=1800, le=2200)
    venue: str = ""
    doi: str | None = None
    snippet: str = ""
    content: str | None = None
    content_access: ContentAccess = ContentAccess.SEARCH_SNIPPET
    source_category: SourceCategory
    provenance: list[SearchProvenance] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def validate_explicit_id(cls, value: str) -> str:
        if value:
            UUID(value)
        return value

    @field_validator("doi", mode="before")
    @classmethod
    def clean_doi(cls, value: object) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        doi = str(value).strip().casefold()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if doi.startswith(prefix):
                doi = doi[len(prefix) :]
        return doi

    @model_validator(mode="after")
    def assign_stable_id(self) -> "ResearchCandidate":
        if not self.id:
            identity = self.doi or self.url.strip().casefold() or self.title.casefold()
            self.id = str(uuid5(NAMESPACE_URL, f"magicforge:research:{identity}"))
        return self


class SearchLedgerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: DiscoveryProvider
    tool_name: str
    query: str
    searched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    result_count: int = Field(ge=0)
    success: bool
    error: str | None = None


class DiscoveryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: ResearchProtocol
    candidates: list[ResearchCandidate]
    search_ledger: list[SearchLedgerEntry]
    duplicate_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
