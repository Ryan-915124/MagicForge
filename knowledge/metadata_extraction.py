"""Per-chunk structured magic metadata extraction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from knowledge.extractors import ExtractedDocument
from knowledge.models import ChunkMetadata, SourceReference
from llm.glm_client import GLMClient


PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "metadata_extractor_prompt.txt"


class MetadataExtractionResult(BaseModel):
    """The exact JSON contract requested from GLM."""

    model_config = ConfigDict(extra="forbid")

    magic_category: str = ""
    techniques: list[str] = Field(default_factory=list)
    psychological_principles: list[str] = Field(default_factory=list)
    performers: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator(
        "techniques", "psychological_principles", "performers", mode="before"
    )
    @classmethod
    def coerce_names(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value


class MetadataExtractor(Protocol):
    def extract(
        self,
        text: str,
        document: ExtractedDocument,
        source_locator: str | None,
    ) -> ChunkMetadata: ...


class DeclaredMetadataExtractor:
    """Deterministic fallback using document metadata only; it is not an LLM."""

    def extract(
        self,
        text: str,
        document: ExtractedDocument,
        source_locator: str | None,
    ) -> ChunkMetadata:
        declared = document.declared_metadata
        sources = _declared_sources(declared.get("sources"))
        sources.insert(
            0,
            SourceReference(
                title=document.title,
                author=document.author,
                locator=source_locator,
            ),
        )
        return ChunkMetadata(
            magic_category=str(declared.get("category") or ""),
            techniques=_as_names(declared.get("technique")),
            psychological_principles=_as_names(declared.get("psychology")),
            performers=_as_names(declared.get("performer")),
            sources=_unique_sources(sources),
            extraction_method="declared",
            confidence=1.0 if declared else None,
        )


class GLMMetadataExtractor:
    """Extract validated metadata with GLM and merge declared provenance."""

    def __init__(self, llm: GLMClient) -> None:
        self.llm = llm
        try:
            self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"could not load metadata extraction prompt: {exc}") from exc
        self.declared_extractor = DeclaredMetadataExtractor()

    def extract(
        self,
        text: str,
        document: ExtractedDocument,
        source_locator: str | None,
    ) -> ChunkMetadata:
        schema = json.dumps(
            MetadataExtractionResult.model_json_schema(), ensure_ascii=False
        )
        prompt = (
            f"Document title: {document.title}\n"
            f"Document author: {document.author or 'unknown'}\n"
            f"Location: {source_locator or 'unknown'}\n\n"
            f"Chunk:\n{text}\n\n"
            f"Return one JSON object matching this schema exactly:\n{schema}"
        )
        extracted = self.llm.generate_structured(
            prompt,
            MetadataExtractionResult,
            system_prompt=self.system_prompt,
            temperature=0.1,
        )
        declared = self.declared_extractor.extract(text, document, source_locator)
        return ChunkMetadata(
            magic_category=extracted.magic_category or declared.magic_category,
            techniques=_unique([*declared.techniques, *extracted.techniques]),
            psychological_principles=_unique(
                [
                    *declared.psychological_principles,
                    *extracted.psychological_principles,
                ]
            ),
            performers=_unique([*declared.performers, *extracted.performers]),
            sources=_unique_sources([*declared.sources, *extracted.sources]),
            extraction_method="merged" if document.declared_metadata else "glm",
            confidence=extracted.confidence,
        )


def _as_names(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _declared_sources(value: Any) -> list[SourceReference]:
    if not value:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    output = []
    for item in value:
        if isinstance(item, str):
            output.append(SourceReference(title=item))
        elif isinstance(item, dict):
            output.append(SourceReference.model_validate(item))
    return output


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            output.append(normalized)
    return output


def _unique_sources(values: list[SourceReference]) -> list[SourceReference]:
    seen: set[tuple[str, str, str]] = set()
    output = []
    for value in values:
        key = (
            value.title.casefold(),
            value.author.casefold(),
            (value.locator or "").casefold(),
        )
        if key not in seen:
            seen.add(key)
            output.append(value)
    return output
