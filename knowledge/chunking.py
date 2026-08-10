"""Format-neutral document chunking with source locator preservation."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from knowledge.extractors import ExtractedDocument


class ChunkingError(ValueError):
    pass


class RawChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    heading: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    source_locator: str | None = None


def chunk_document(
    document: ExtractedDocument,
    *,
    chunk_size: int = 1_200,
    chunk_overlap: int = 150,
) -> list[RawChunk]:
    _validate_limits(chunk_size, chunk_overlap)
    chunks: list[RawChunk] = []
    for section in document.sections:
        prefix = f"{section.heading}\n\n" if section.heading else ""
        available = max(1, chunk_size - len(prefix))
        overlap = min(chunk_overlap, max(0, available - 1))
        for window in text_windows(section.text, available, overlap):
            chunks.append(
                RawChunk(
                    text=(prefix + window).strip(),
                    chunk_index=len(chunks),
                    heading=section.heading,
                    page_number=section.page_number,
                    source_locator=section.locator,
                )
            )
    if not chunks:
        raise ChunkingError(f"{document.path} produced no chunks")
    return chunks


def text_windows(text: str, size: int, overlap: int) -> Iterable[str]:
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("\n\n", start, end), text.rfind(" ", start, end))
            if boundary > start:
                end = boundary
        window = text[start:end].strip()
        if window:
            yield window
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        while next_start < end and text[next_start].isspace():
            next_start += 1
        start = next_start


def _validate_limits(chunk_size: int, chunk_overlap: int) -> None:
    if chunk_size <= 0:
        raise ChunkingError("chunk_size must be positive")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ChunkingError("chunk_overlap must be non-negative and smaller than chunk_size")
