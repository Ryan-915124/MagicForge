"""Tavily MCP adapter for practitioner and general web discovery."""

from __future__ import annotations

from research.models import (
    DiscoveryProvider,
    ResearchCandidate,
    SearchProvenance,
    SourceCategory,
)
from research.search.base import (
    MCPToolExecutor,
    extract_result_items,
    first_text,
    parse_authors,
    parse_doi,
    parse_year,
)


class TavilyPractitionerSearch:
    provider = DiscoveryProvider.TAVILY
    source_category = SourceCategory.PRACTITIONER
    tool_name = "tavily_search"

    def __init__(self, executor: MCPToolExecutor, *, tool_name: str | None = None) -> None:
        self.executor = executor
        if tool_name:
            self.tool_name = tool_name

    def search(self, query: str, limit: int) -> list[ResearchCandidate]:
        practitioner_query = (
            f"Magic practitioner sources, performance theory, interviews, historical "
            f"archives, and credited essays about: {query}"
        )
        raw = self.executor.call_tool(
            self.tool_name,
            {
                "query": practitioner_query,
                "max_results": limit,
                "search_depth": "advanced",
                "topic": "general",
            },
        )
        candidates = []
        for rank, item in enumerate(extract_result_items(raw), start=1):
            title = first_text(item, "title", "name")
            url = first_text(item, "url")
            if not title or not url:
                continue
            candidates.append(
                ResearchCandidate(
                    title=title,
                    url=url,
                    authors=parse_authors(item),
                    published_year=parse_year(
                        item.get("published_date") or item.get("publishedDate")
                    ),
                    venue=first_text(item, "site_name", "publisher"),
                    doi=parse_doi(item, url),
                    snippet=first_text(item, "content", "raw_content", "summary"),
                    source_category=self.source_category,
                    provenance=[
                        SearchProvenance(
                            provider=self.provider,
                            tool_name=self.tool_name,
                            query=query,
                            provider_result_id=first_text(item, "id") or None,
                            rank=rank,
                        )
                    ],
                )
            )
        return candidates
