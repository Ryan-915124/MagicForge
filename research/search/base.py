"""Interfaces and response normalization for MCP search tools."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Protocol

from research.models import DiscoveryProvider, ResearchCandidate, SourceCategory


class ResearchSearchError(RuntimeError):
    pass


class MCPToolExecutor(Protocol):
    """Bridge supplied by the MCP host; transport details stay outside the domain."""

    def call_tool(self, tool_name: str, arguments: Mapping[str, Any]) -> Any: ...


class ResearchSearchProvider(Protocol):
    provider: DiscoveryProvider
    source_category: SourceCategory
    tool_name: str

    def search(self, query: str, limit: int) -> list[ResearchCandidate]: ...


def extract_result_items(raw: Any) -> list[dict[str, Any]]:
    """Unwrap common MCP CallToolResult and provider response envelopes."""

    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, Mapping)]
    if not isinstance(raw, Mapping):
        raise ResearchSearchError("MCP search returned an unsupported response type")

    for key in ("results", "data", "items"):
        value = raw.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    for key in ("structuredContent", "result"):
        value = raw.get(key)
        if isinstance(value, Mapping):
            try:
                return extract_result_items(value)
            except ResearchSearchError:
                pass

    content = raw.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, Mapping) or not isinstance(block.get("text"), str):
                continue
            try:
                parsed = json.loads(block["text"])
            except json.JSONDecodeError:
                parsed_items = _parse_labeled_text_results(block["text"])
                if parsed_items:
                    return parsed_items
            else:
                try:
                    return extract_result_items(parsed)
                except ResearchSearchError:
                    continue
    raise ResearchSearchError("MCP search response contained no structured result list")


def first_text(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            joined = "\n".join(str(part).strip() for part in value if str(part).strip())
            if joined:
                return joined
    return ""


def parse_year(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value)
    for token in text.replace("/", "-").split("-"):
        if len(token) == 4 and token.isdigit():
            year = int(token)
            if 1800 <= year <= 2200:
                return year
    return None


def parse_authors(item: Mapping[str, Any]) -> list[str]:
    value = item.get("authors") or item.get("author") or []
    if isinstance(value, str):
        return [
            part.strip()
            for part in value.split(",")
            if part.strip() and part.strip().casefold() not in {"n/a", "unknown", "none"}
        ]
    if isinstance(value, list):
        output = []
        for author in value:
            if isinstance(author, Mapping):
                name = author.get("name") or author.get("author")
                if name:
                    output.append(str(name).strip())
            elif str(author).strip():
                output.append(str(author).strip())
        return output
    return []


def parse_doi(item: Mapping[str, Any], url: str = "") -> str | None:
    explicit = first_text(item, "doi")
    haystack = explicit or url
    match = re.search(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", haystack, re.IGNORECASE)
    return match.group(0).rstrip(".,;").casefold() if match else None


def _parse_labeled_text_results(text: str) -> list[dict[str, Any]]:
    """Parse the plain labelled blocks returned by the Exa MCP server."""

    blocks = re.split(r"(?m)(?=^Title:\s*)", text.strip())
    results = []
    for block in blocks:
        if not block.startswith("Title:"):
            continue
        labels: dict[str, str] = {}
        current_key: str | None = None
        body: list[str] = []
        for line in block.splitlines():
            match = re.match(r"^(Title|URL|Published|Author|Highlights):\s*(.*)$", line)
            if match:
                current_key = match.group(1).casefold()
                labels[current_key] = match.group(2).strip()
                continue
            if current_key == "highlights":
                body.append(line)
        if body:
            labels["highlights"] = "\n".join(
                [labels.get("highlights", ""), *body]
            ).strip()
        if labels.get("title") and labels.get("url"):
            results.append(
                {
                    "title": labels["title"],
                    "url": labels["url"],
                    "publishedDate": labels.get("published", ""),
                    "author": labels.get("author", ""),
                    "highlights": labels.get("highlights", ""),
                }
            )
    return results
