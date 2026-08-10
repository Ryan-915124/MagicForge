"""Dual-stream discovery, failure visibility, and deterministic deduplication."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from research.models import (
    DiscoveryBatch,
    ResearchCandidate,
    ResearchProtocol,
    SearchLedgerEntry,
)
from research.search.base import ResearchSearchProvider


class ResearchSearchCoordinator:
    def __init__(
        self,
        academic_provider: ResearchSearchProvider,
        practitioner_provider: ResearchSearchProvider,
    ) -> None:
        self.academic_provider = academic_provider
        self.practitioner_provider = practitioner_provider

    def discover(self, protocol: ResearchProtocol) -> DiscoveryBatch:
        candidates: list[ResearchCandidate] = []
        ledger: list[SearchLedgerEntry] = []
        streams = (
            (self.academic_provider, protocol.academic_queries),
            (self.practitioner_provider, protocol.practitioner_queries),
        )
        for provider, queries in streams:
            for query in queries:
                try:
                    found = provider.search(query, protocol.max_results_per_query)
                    candidates.extend(found)
                    ledger.append(
                        SearchLedgerEntry(
                            provider=provider.provider,
                            tool_name=provider.tool_name,
                            query=query,
                            result_count=len(found),
                            success=True,
                        )
                    )
                except Exception as exc:
                    ledger.append(
                        SearchLedgerEntry(
                            provider=provider.provider,
                            tool_name=provider.tool_name,
                            query=query,
                            result_count=0,
                            success=False,
                            error=str(exc),
                        )
                    )
        unique = _deduplicate(candidates)
        return DiscoveryBatch(
            protocol=protocol,
            candidates=unique,
            search_ledger=ledger,
            duplicate_count=len(candidates) - len(unique),
        )


def _deduplicate(candidates: list[ResearchCandidate]) -> list[ResearchCandidate]:
    by_key: dict[str, ResearchCandidate] = {}
    for candidate in candidates:
        key = _candidate_key(candidate)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = candidate
            continue
        merged_provenance = [*existing.provenance]
        known = {
            (item.provider, item.query, item.provider_result_id)
            for item in merged_provenance
        }
        for item in candidate.provenance:
            marker = (item.provider, item.query, item.provider_result_id)
            if marker not in known:
                merged_provenance.append(item)
                known.add(marker)
        by_key[key] = existing.model_copy(
            update={
                "provenance": merged_provenance,
                "doi": existing.doi or candidate.doi,
                "authors": existing.authors or candidate.authors,
                "published_year": existing.published_year or candidate.published_year,
                "venue": existing.venue or candidate.venue,
                "snippet": existing.snippet or candidate.snippet,
            }
        )
    return list(by_key.values())


def _candidate_key(candidate: ResearchCandidate) -> str:
    if candidate.doi:
        return f"doi:{candidate.doi}"
    canonical_url = _canonical_url(candidate.url)
    if canonical_url:
        return f"url:{canonical_url}"
    normalized_title = re.sub(r"[^a-z0-9]+", "", candidate.title.casefold())
    return f"title:{normalized_title}"


def _canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if not parts.netloc:
        return url.strip().casefold()
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
    ]
    return urlunsplit(
        (
            parts.scheme.casefold() or "https",
            parts.netloc.casefold(),
            parts.path.rstrip("/"),
            urlencode(query),
            "",
        )
    )
