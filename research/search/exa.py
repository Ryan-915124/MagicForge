"""Exa MCP adapter for academic discovery."""

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


class ExaAcademicSearch:
    provider = DiscoveryProvider.EXA
    source_category = SourceCategory.ACADEMIC
    tool_name = "web_search_exa"

    def __init__(self, executor: MCPToolExecutor, *, tool_name: str | None = None) -> None:
        self.executor = executor
        if tool_name:
            self.tool_name = tool_name

    def search(self, query: str, limit: int) -> list[ResearchCandidate]:
        academic_query = (
            "Peer-reviewed academic papers, research articles, and primary scholarly "
            f"sources addressing: {query}. Prefer records with DOI and author metadata."
        )
        raw = self.executor.call_tool(
            self.tool_name,
            {"query": academic_query, "numResults": limit},
        )
        candidates = []
        for rank, item in enumerate(extract_result_items(raw), start=1):
            title = first_text(item, "title", "name")
            url = first_text(item, "url", "id")
            if not title or not url:
                continue
            candidates.append(
                ResearchCandidate(
                    title=title,
                    url=url,
                    authors=parse_authors(item),
                    published_year=parse_year(
                        item.get("publishedDate") or item.get("published_date")
                    ),
                    venue=first_text(item, "venue", "journal", "publisher"),
                    doi=parse_doi(item, url),
                    snippet=first_text(item, "text", "highlights", "summary"),
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
