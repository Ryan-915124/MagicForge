"""MCP-backed content retrieval without confusing snippets with full text."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from research.models import ContentAccess, DiscoveryProvider, ResearchCandidate
from research.search.base import MCPToolExecutor, ResearchSearchError, extract_result_items, first_text


class MCPContentFetcher:
    """Fetch readable source text through the same provider that discovered it."""

    def __init__(
        self,
        executor: MCPToolExecutor,
        provider: DiscoveryProvider,
        *,
        max_characters: int = 40_000,
        tool_name: str | None = None,
    ) -> None:
        self.executor = executor
        self.provider = provider
        self.max_characters = max_characters
        self.tool_name = tool_name or (
            "web_fetch_exa" if provider == DiscoveryProvider.EXA else "tavily_extract"
        )

    def fetch(self, candidate: ResearchCandidate) -> ResearchCandidate:
        arguments: dict[str, Any]
        if self.provider == DiscoveryProvider.EXA:
            arguments = {
                "urls": [candidate.url],
                "maxCharacters": self.max_characters,
            }
        else:
            arguments = {
                "urls": [candidate.url],
                "extract_depth": "advanced",
            }
        raw = self.executor.call_tool(self.tool_name, arguments)
        text = _extract_content(raw, candidate.url)
        if not text:
            raise ResearchSearchError(
                f"{self.tool_name} returned no readable content for {candidate.url}"
            )
        # MCP extraction may be truncated, so it is deliberately not labelled full text.
        return candidate.model_copy(
            update={"content": text, "content_access": ContentAccess.WEB_EXTRACT}
        )


def _extract_content(raw: Any, expected_url: str) -> str:
    try:
        items = extract_result_items(raw)
    except ResearchSearchError:
        items = []
    for item in items:
        item_url = first_text(item, "url", "source_url")
        if item_url and item_url.rstrip("/") != expected_url.rstrip("/"):
            continue
        text = first_text(
            item,
            "content",
            "raw_content",
            "text",
            "markdown",
            "summary",
        )
        if text:
            return text
    if isinstance(raw, Mapping):
        content = raw.get("content")
        if isinstance(content, list):
            plain_blocks = [
                str(block.get("text")).strip()
                for block in content
                if isinstance(block, Mapping) and str(block.get("text") or "").strip()
            ]
            if plain_blocks:
                return "\n\n".join(plain_blocks)
        return first_text(raw, "content", "raw_content", "text", "markdown")
    return ""
