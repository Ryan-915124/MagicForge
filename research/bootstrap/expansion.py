"""Compile broad MCP discovery batches into a screened incremental source run.

This module is deliberately upstream of extraction.  It does not create
Evidence Cards, entities, relationships, manifests, or Qdrant points.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from research.models import (
    ContentAccess,
    DiscoveryProvider,
    ResearchCandidate,
    SearchProvenance,
    SourceCategory,
)


DEFAULT_RUN = Path("research/runs/bootstrap-002")
EXISTING_RUNS = (
    Path("research/runs/magicforge-corpus-run-001"),
    Path("research/runs/bootstrap-001"),
)

_DOI = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
_ISBN = re.compile(
    r"(?:isbn(?:-1[03])?\s*[:#]?\s*)?((?:97[89][ -]?)?\d[\d -]{8,15}[\dxX])",
    re.IGNORECASE,
)
_RELEVANCE = {
    "attention",
    "audience",
    "change blindness",
    "choice",
    "cognition",
    "conjuring",
    "creativity",
    "deception",
    "dramaturgy",
    "emotion",
    "expertise",
    "false memory",
    "forcing",
    "gaze",
    "illusion",
    "impossible",
    "inattentional blindness",
    "magic",
    "magician",
    "memory",
    "mentalism",
    "misdirection",
    "motor learning",
    "narrative",
    "perception",
    "performance",
    "prediction error",
    "sleight",
    "stagecraft",
    "storytelling",
    "surprise",
    "theater",
    "theatre",
    "timing",
    "trust",
    "wonder",
}
_PRACTITIONER_NAMES = {
    "ascanio",
    "darwin ortiz",
    "dai vernon",
    "juan tamariz",
    "slydini",
    "tommy wonder",
}
_HARD_EXCLUDED_DOMAINS = {
    "amazon.com",
    "ebay.com",
    "facebook.com",
    "instagram.com",
    "pinterest.com",
    "reddit.com",
    "scribd.com",
    "tiktok.com",
    "youtube.com",
}
_PIRACY_TERMS = {
    "free download pdf",
    "pdfcoffee",
    "pdfcoffee.com",
    "pdf drive",
    "scribd",
    "torrent",
}
_LOW_INFORMATION_TERMS = {
    "add to cart",
    "buy now",
    "product page",
    "shopping results",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    parser.add_argument(
        "--existing-run", type=Path, action="append", default=list(EXISTING_RUNS)
    )
    args = parser.parse_args()
    report = compile_discovery(args.run, args.existing_run)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def compile_discovery(run: Path, existing_runs: list[Path]) -> dict[str, object]:
    raw_rows = [
        row
        for path in sorted((run / "discovery" / "raw").glob("*.json"))
        for row in json.loads(path.read_text(encoding="utf-8"))["results"]
    ]
    existing = [
        candidate
        for existing_run in existing_runs
        for candidate in _load_candidates(existing_run)
    ]
    existing_keys = {key for item in existing for key in _identity_keys(item)}

    compiled: dict[str, ResearchCandidate] = {}
    excluded = []
    duplicate_rows = 0
    existing_duplicates = 0
    for row in raw_rows:
        candidate = _candidate_from_row(row)
        decision, reason = _screen(candidate)
        if not decision:
            excluded.append(_exclusion(candidate, reason))
            continue
        keys = _identity_keys(candidate)
        if keys & existing_keys:
            existing_duplicates += 1
            excluded.append(_exclusion(candidate, "duplicate_of_existing_corpus"))
            continue
        existing_match = next(
            (
                identity
                for identity, saved in compiled.items()
                if keys & _identity_keys(saved)
            ),
            None,
        )
        if existing_match is not None:
            duplicate_rows += 1
            compiled[existing_match] = _merge(compiled[existing_match], candidate)
            continue
        identity = sorted(keys)[0]
        compiled[identity] = candidate

    candidates = sorted(
        compiled.values(),
        key=lambda item: (
            item.source_category.value,
            _domain_for_queries(p.query for p in item.provenance),
            item.title.casefold(),
        ),
    )
    academic = [
        item.model_dump(mode="json")
        for item in candidates
        if item.source_category == SourceCategory.ACADEMIC
    ]
    practitioner = [
        item.model_dump(mode="json")
        for item in candidates
        if item.source_category == SourceCategory.PRACTITIONER
    ]
    output_candidates = run / "candidates"
    output_reports = run / "reports"
    output_candidates.mkdir(parents=True, exist_ok=True)
    output_reports.mkdir(parents=True, exist_ok=True)
    _write(output_candidates / "academic-candidates.json", academic)
    _write(output_candidates / "practitioner-candidates.json", practitioner)
    _write(
        output_candidates / "screening-index.json",
        [
            {
                "candidate_id": item.id,
                "domain": _domain_for_queries(
                    provenance.query for provenance in item.provenance
                ),
                "screening_status": "included_for_access_check",
                "screening_reason": (
                    "relevant discovery record; exact content still required"
                ),
                "isbn": _isbn(item),
                "human_verified": False,
                "provider_count": len({p.provider for p in item.provenance}),
                "query_count": len({p.query for p in item.provenance}),
            }
            for item in candidates
        ],
    )
    _write(run / "discovery" / "excluded.json", excluded)
    queries = {
        (row["provider"], row["query"])
        for row in raw_rows
    }
    domain_counts = Counter(
        _domain_for_queries(p.query for p in item.provenance) for item in candidates
    )
    report = {
        "run_id": run.name,
        "compiled_at": datetime.now(UTC).isoformat(),
        "raw_records": len(raw_rows),
        "queries_executed": len(queries),
        "academic_queries": sum(provider == "exa" for provider, _ in queries),
        "practitioner_queries": sum(provider == "tavily" for provider, _ in queries),
        "duplicate_rows_removed": duplicate_rows,
        "duplicates_against_existing_54": existing_duplicates,
        "screening_exclusions": len(excluded) - existing_duplicates,
        "included_candidates": len(candidates),
        "academic_candidates": len(academic),
        "practitioner_candidates": len(practitioner),
        "domain_coverage": dict(sorted(domain_counts.items())),
        "state": "discovery_only_not_extracted",
    }
    _write(output_reports / "discovery-report.json", report)
    return report


def _candidate_from_row(row: dict[str, object]) -> ResearchCandidate:
    provider = DiscoveryProvider(str(row["provider"]))
    category = (
        SourceCategory.ACADEMIC
        if provider == DiscoveryProvider.EXA
        else SourceCategory.PRACTITIONER
    )
    text = f"{row.get('url') or ''} {row.get('snippet') or ''}"
    doi_match = _DOI.search(text)
    author = str(row.get("author") or "").strip()
    authors = []
    if author and author.casefold() not in {"n/a", "unknown", "none"}:
        authors = [
            value.strip()
            for value in re.split(r"\s*(?:,|;|\band\b)\s*", author)
            if value.strip()
        ]
    year_match = re.search(r"\b(18|19|20|21)\d{2}\b", str(row.get("published") or ""))
    return ResearchCandidate(
        title=str(row["title"]).strip(),
        url=_canonical_url(str(row["url"])),
        authors=authors,
        published_year=int(year_match.group(0)) if year_match else None,
        venue=_venue_from_url(str(row["url"])),
        doi=doi_match.group(0).rstrip(".,;").casefold() if doi_match else None,
        snippet=str(row.get("snippet") or "").strip(),
        content_access=ContentAccess.SEARCH_SNIPPET,
        source_category=category,
        provenance=[
            SearchProvenance(
                provider=provider,
                tool_name=(
                    "web_search_exa"
                    if provider == DiscoveryProvider.EXA
                    else "tavily_search"
                ),
                query=str(row["query"]),
                retrieved_at=str(row["retrieved_at"]),
                rank=int(row["rank"]),
            )
        ],
    )


def _screen(candidate: ResearchCandidate) -> tuple[bool, str]:
    parsed = urlsplit(candidate.url)
    host = parsed.netloc.casefold().removeprefix("www.")
    text = f"{candidate.title} {candidate.snippet}".casefold()
    if any(host == domain or host.endswith(f".{domain}") for domain in _HARD_EXCLUDED_DOMAINS):
        return False, "excluded_social_commerce_or_aggregator_domain"
    if any(term in text or term in candidate.url.casefold() for term in _PIRACY_TERMS):
        return False, "suspected_unauthorized_or_low_provenance_copy"
    if len(candidate.snippet) < 80:
        return False, "insufficient_discovery_content"
    relevant = sum(term in text for term in _RELEVANCE)
    named = any(name in text for name in _PRACTITIONER_NAMES)
    if relevant == 0 and not named:
        return False, "off_scope"
    if candidate.source_category == SourceCategory.PRACTITIONER:
        if any(term in text for term in _LOW_INFORMATION_TERMS) and relevant < 2:
            return False, "commercial_page_without_substantive_theory"
        if not any(
            term in text
            for term in ("magic", "magician", "conjuring", "mentalism", "illusion")
        ) and not named:
            return False, "not_magic_practitioner_material"
    return True, "included"


def _merge(first: ResearchCandidate, second: ResearchCandidate) -> ResearchCandidate:
    provenance = [*first.provenance]
    seen = {(p.provider, p.query, p.rank) for p in provenance}
    for item in second.provenance:
        marker = (item.provider, item.query, item.rank)
        if marker not in seen:
            provenance.append(item)
            seen.add(marker)
    preferred = first
    if second.doi and not first.doi:
        preferred = second
    elif len(second.snippet) > len(first.snippet):
        preferred = second
    return preferred.model_copy(
        update={
            "provenance": provenance,
            "doi": first.doi or second.doi,
            "authors": first.authors or second.authors,
            "published_year": first.published_year or second.published_year,
            "venue": first.venue or second.venue,
            "snippet": max((first.snippet, second.snippet), key=len),
        }
    )


def _identity_keys(candidate: ResearchCandidate) -> set[str]:
    keys = {f"url:{_canonical_url(candidate.url)}"}
    if candidate.doi:
        keys.add(f"doi:{candidate.doi.casefold()}")
    isbn = _isbn(candidate)
    if isbn:
        keys.add(f"isbn:{isbn}")
    title = re.sub(r"[^a-z0-9]+", "", candidate.title.casefold())
    author = re.sub(
        r"[^a-z0-9]+", "", candidate.authors[0].casefold()
    ) if candidate.authors else ""
    keys.add(f"title_author:{title}:{author}")
    keys.add(f"title:{title}")
    return keys


def _isbn(candidate: ResearchCandidate) -> str | None:
    match = _ISBN.search(f"{candidate.title} {candidate.snippet}")
    if not match:
        return None
    value = re.sub(r"[^0-9x]", "", match.group(1).casefold())
    return value if len(value) in {10, 13} else None


def _domain_for_queries(queries) -> str:
    text = " ".join(queries).casefold()
    priorities = (
        ("magic_history", ("history", "historical", "archive", "tradition")),
        ("technique_knowledge", ("card magic", "coin magic", "sleight", "palms", "stage magic", "mentalism")),
        ("expertise_training", ("expertise", "practice", "training", "skill acquisition", "creativity", "improvisation")),
        ("performance_theory", ("performance theory", "dramaturgy", "storytelling", "character", "stagecraft", "audience relationship")),
        ("misdirection_theory", ("misdirection", "covert attention", "temporal attention")),
        ("classical_magic_theory", tuple(_PRACTITIONER_NAMES)),
    )
    for domain, markers in priorities:
        if any(marker in text for marker in markers):
            return domain
    return "cognitive_science"


def _load_candidates(run: Path) -> list[ResearchCandidate]:
    values = []
    for name in ("academic-candidates.json", "practitioner-candidates.json"):
        path = run / "candidates" / name
        if path.exists():
            values.extend(json.loads(path.read_text(encoding="utf-8")))
    return [ResearchCandidate.model_validate(value) for value in values]


def _canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
    ]
    return urlunsplit(
        (
            (parts.scheme or "https").casefold(),
            parts.netloc.casefold(),
            parts.path.rstrip("/"),
            urlencode(query),
            "",
        )
    )


def _venue_from_url(url: str) -> str:
    return urlsplit(url).netloc.casefold().removeprefix("www.")


def _exclusion(candidate: ResearchCandidate, reason: str) -> dict[str, object]:
    return {
        "candidate_id": candidate.id,
        "title": candidate.title,
        "url": candidate.url,
        "source_category": candidate.source_category.value,
        "reason": reason,
        "provenance": [item.model_dump(mode="json") for item in candidate.provenance],
    }


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
