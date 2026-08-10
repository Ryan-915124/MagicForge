"""Document extraction adapters for Markdown, text, and PDF files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field


class DocumentExtractionError(ValueError):
    pass


class ExtractedSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    heading: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    locator: str | None = None


class ExtractedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    media_type: str
    title: str
    author: str = ""
    sections: list[ExtractedSection] = Field(min_length=1)
    declared_metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentExtractor(Protocol):
    suffixes: frozenset[str]

    def extract(self, path: Path) -> ExtractedDocument: ...


class MarkdownExtractor:
    suffixes = frozenset({".md", ".markdown"})

    def extract(self, path: Path) -> ExtractedDocument:
        raw = _read_text(path)
        metadata, markdown = parse_front_matter(raw, path)
        sections = _markdown_sections(markdown)
        if not sections:
            raise DocumentExtractionError(f"{path} has no ingestible content")
        title = str(metadata.get("title") or _first_heading(markdown) or path.stem)
        return ExtractedDocument(
            path=str(path),
            media_type="text/markdown",
            title=title,
            author=str(metadata.get("author") or ""),
            sections=sections,
            declared_metadata=metadata,
        )


class TextExtractor:
    suffixes = frozenset({".txt", ".text"})

    def extract(self, path: Path) -> ExtractedDocument:
        text = _read_text(path).strip()
        if not text:
            raise DocumentExtractionError(f"{path} has no ingestible content")
        return ExtractedDocument(
            path=str(path),
            media_type="text/plain",
            title=path.stem,
            sections=[ExtractedSection(text=text, locator="text")],
        )


class PDFExtractor:
    suffixes = frozenset({".pdf"})

    def extract(self, path: Path) -> ExtractedDocument:
        try:
            import pdfplumber
        except ImportError as exc:  # pragma: no cover - installation problem
            raise DocumentExtractionError(
                "pdfplumber is not installed; run `pip install -r requirements.txt`"
            ) from exc

        try:
            with pdfplumber.open(path) as pdf:
                pdf_metadata = pdf.metadata or {}
                sections = []
                for page_number, page in enumerate(pdf.pages, start=1):
                    text = (page.extract_text() or "").strip()
                    if text:
                        sections.append(
                            ExtractedSection(
                                text=text,
                                page_number=page_number,
                                locator=f"page {page_number}",
                            )
                        )
        except Exception as exc:
            raise DocumentExtractionError(f"could not extract PDF {path}: {exc}") from exc

        if not sections:
            raise DocumentExtractionError(
                f"{path} contains no extractable text; scanned PDFs require OCR"
            )
        title = str(pdf_metadata.get("Title") or pdf_metadata.get("title") or path.stem)
        author = str(pdf_metadata.get("Author") or pdf_metadata.get("author") or "")
        return ExtractedDocument(
            path=str(path),
            media_type="application/pdf",
            title=title,
            author=author,
            sections=sections,
            declared_metadata={"pdf_metadata": pdf_metadata},
        )


class ExtractionRegistry:
    def __init__(self, extractors: list[DocumentExtractor] | None = None) -> None:
        adapters = extractors or [MarkdownExtractor(), TextExtractor(), PDFExtractor()]
        self._by_suffix = {
            suffix: extractor
            for extractor in adapters
            for suffix in extractor.suffixes
        }

    @property
    def supported_suffixes(self) -> frozenset[str]:
        return frozenset(self._by_suffix)

    def extract(self, path: str | Path) -> ExtractedDocument:
        document_path = Path(path)
        extractor = self._by_suffix.get(document_path.suffix.casefold())
        if extractor is None:
            supported = ", ".join(sorted(self.supported_suffixes))
            raise DocumentExtractionError(
                f"unsupported file type {document_path.suffix!r}; expected {supported}"
            )
        return extractor.extract(document_path)


def parse_front_matter(raw: str, path: Path) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---"):
        return {}, raw
    match = re.match(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|\Z)", raw, re.DOTALL)
    if not match:
        raise DocumentExtractionError(f"invalid YAML front matter in {path}")
    try:
        loaded = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise DocumentExtractionError(f"invalid YAML front matter in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise DocumentExtractionError(f"front matter in {path} must be a mapping")
    return loaded, raw[match.end() :]


def _markdown_sections(markdown: str) -> list[ExtractedSection]:
    sections: list[ExtractedSection] = []
    heading: str | None = None
    lines: list[str] = []
    for line in markdown.splitlines():
        heading_match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading_match:
            _append_section(sections, heading, lines)
            heading = heading_match.group(1).strip()
            lines = []
        else:
            lines.append(line)
    _append_section(sections, heading, lines)
    return sections


def _append_section(
    sections: list[ExtractedSection], heading: str | None, lines: list[str]
) -> None:
    text = "\n".join(lines).strip()
    if text:
        sections.append(
            ExtractedSection(text=text, heading=heading, locator=heading or "document")
        )


def _first_heading(markdown: str) -> str:
    match = re.search(r"^#\s+(.+?)\s*$", markdown, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise DocumentExtractionError(f"could not read {path}: {exc}") from exc
