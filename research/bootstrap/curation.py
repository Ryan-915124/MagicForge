"""Select a quality-balanced Bootstrap 002 processing corpus.

Selection is deterministic, access-gated, and source-type aware.  Discovery
provider never determines whether a source is scientific or practitioner
knowledge; scholarly publishing signals do.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import TypeAdapter

from research.bootstrap.expansion import _domain_for_queries
from research.models import ResearchCandidate, SourceCategory


DEFAULT_RUN = Path("research/runs/bootstrap-002")
ACADEMIC_TARGET = 90
PRACTITIONER_TARGET = 115

_SCHOLARLY_HOSTS = {
    "annualreviews.org",
    "ar5iv.labs.arxiv.org",
    "cambridge.org",
    "dl.acm.org",
    "doi.org",
    "eprints.whiterose.ac.uk",
    "export.arxiv.org",
    "frontiersin.org",
    "journalofperformancemagic.org.uk",
    "journals.plos.org",
    "journals.sagepub.com",
    "link.springer.com",
    "mdpi.com",
    "nature.com",
    "onlinelibrary.wiley.com",
    "papers.ssrn.com",
    "pearl.plymouth.ac.uk",
    "pmc.ncbi.nlm.nih.gov",
    "preview-www.nature.com",
    "pubmed.ncbi.nlm.nih.gov",
    "pure.ed.ac.uk",
    "research.gold.ac.uk",
    "scholarworks.sjsu.edu",
    "sciencedirect.com",
    "tandfonline.com",
}
_TRUSTED_PRACTITIONER_HOSTS = {
    "americanmuseumofmagic.com",
    "archive.org",
    "archive.senatehouselibrary.ac.uk",
    "archiveshub.jisc.ac.uk",
    "atlasobscura.com",
    "britannica.com",
    "chambermagic.com",
    "christopherhowell.net",
    "coinvanish.com",
    "conjuringarchive.com",
    "conjuringarts.org",
    "discourseinmagic.com",
    "geniimagazine.com",
    "gutenberg.org",
    "magicana.com",
    "michaelvincentmagic.com",
    "oneahead.com",
    "pbs.org",
    "smithsonianmag.com",
    "themagiccircle.co.uk",
    "theory11.com",
    "vanishingincmagic.com",
}
_REJECT_HOSTS = {
    "academia.edu",
    "dokumen.pub",
    "en.wikipedia.org",
    "mapquest.com",
    "medium.com",
    "playingcardforum.com",
    "playingcards.wikidot.com",
    "stock.zieduekspresis.lv",
    "themagiciansforum.com",
    "wiki.geniimagazine.com",
}
_REJECT_TITLE = re.compile(
    r"^(?:n/?a|magic \(supernatural\)|magic tutorial|mentalism)$", re.IGNORECASE
)
_MAGIC_MARKERS = {
    "audience",
    "card magic",
    "conjuring",
    "illusion",
    "magic",
    "magician",
    "mentalism",
    "misdirection",
    "performance",
    "sleight",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    args = parser.parse_args()
    report = curate(args.run)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def curate(run: Path) -> dict[str, object]:
    candidates = _load_candidates(run)
    checks = {
        item["candidate_id"]: item
        for item in json.loads((run / "sources" / "access-checks.json").read_text())
    }
    content = json.loads((run / "sources" / "source-content.json").read_text())[
        "sources"
    ]
    content_by_id = {item["candidate_id"]: item["content"] for item in content.values()}
    included: list[tuple[ResearchCandidate, str, int]] = []
    excluded = []
    for candidate in candidates:
        check = checks.get(candidate.id)
        if not check or check["access_status"] != "readable_exact_content":
            excluded.append(_decision(candidate, "unavailable_exact_content", 0))
            continue
        category = _classify(candidate)
        normalized = candidate.model_copy(update={"source_category": category})
        accepted, reason = _quality_gate(normalized, content_by_id[candidate.id])
        score = _score(normalized, content_by_id[candidate.id])
        if not accepted:
            excluded.append(_decision(normalized, reason, score))
            continue
        domain = _domain_for_queries(item.query for item in normalized.provenance)
        included.append((normalized, domain, score))

    academics = _select(
        [item for item in included if item[0].source_category == SourceCategory.ACADEMIC],
        ACADEMIC_TARGET,
    )
    practitioners = _select_balanced(
        [
            item
            for item in included
            if item[0].source_category == SourceCategory.PRACTITIONER
        ],
        PRACTITIONER_TARGET,
    )
    selected = [*academics, *practitioners]
    selected_ids = {item[0].id for item in selected}
    for candidate, _, score in included:
        if candidate.id not in selected_ids:
            excluded.append(_decision(candidate, "quality_rank_below_run_quota", score))

    processing = run / "processing" / "candidates"
    processing.mkdir(parents=True, exist_ok=True)
    _write(
        processing / "academic-candidates.json",
        [item[0].model_dump(mode="json") for item in academics],
    )
    _write(
        processing / "practitioner-candidates.json",
        [item[0].model_dump(mode="json") for item in practitioners],
    )
    _write(
        run / "candidates" / "processing-queue.json",
        [
            {
                "candidate_id": candidate.id,
                "title": candidate.title,
                "url": candidate.url,
                "source_category": candidate.source_category.value,
                "domain": domain,
                "quality_score": score,
                "human_verified": False,
            }
            for candidate, domain, score in selected
        ],
    )
    _write(run / "candidates" / "curation-excluded.json", excluded)
    domains = Counter(domain for _, domain, _ in selected)
    report = {
        "run_id": run.name,
        "curated_at": datetime.now(UTC).isoformat(),
        "access_eligible_before_quality_curation": len(included),
        "selected_for_processing": len(selected),
        "academic_selected": len(academics),
        "practitioner_selected": len(practitioners),
        "quality_or_quota_exclusions": len(excluded),
        "domain_coverage": dict(sorted(domains.items())),
        "human_verified": False,
        "state": "curated_pending_metadata_verification",
    }
    _write(run / "reports" / "curation-report.json", report)
    return report


def _classify(candidate: ResearchCandidate) -> SourceCategory:
    host = _host(candidate.url)
    text = f"{candidate.title} {candidate.url} {candidate.snippet}".casefold()
    if candidate.doi or host in _SCHOLARLY_HOSTS:
        return SourceCategory.ACADEMIC
    if any(marker in text for marker in ("journal", "doi.org/10.", "peer-reviewed")):
        return SourceCategory.ACADEMIC
    return SourceCategory.PRACTITIONER


def _quality_gate(candidate: ResearchCandidate, content: str) -> tuple[bool, str]:
    host = _host(candidate.url)
    title = candidate.title.strip()
    text = f"{title} {content[:4000]}".casefold()
    if not host or host in _REJECT_HOSTS:
        return False, "low_provenance_or_aggregator_host"
    if _REJECT_TITLE.fullmatch(title):
        return False, "generic_or_missing_title"
    if any(value in text for value in ("free download pdf", "pdfcoffee", "torrent")):
        return False, "suspected_unauthorized_copy"
    relevance = sum(marker in text for marker in _MAGIC_MARKERS)
    if relevance == 0:
        return False, "no_magic_or_performance_relevance_in_exact_content"
    if candidate.source_category == SourceCategory.PRACTITIONER:
        commerce = sum(
            marker in text
            for marker in ("add to cart", "buy now", "customers also bought", "sale price")
        )
        if commerce >= 2 and len(content) < 4_000:
            return False, "commercial_listing_without_substantive_practice_content"
    return True, "quality_gate_passed"


def _score(candidate: ResearchCandidate, content: str) -> int:
    host = _host(candidate.url)
    text = f"{candidate.title} {content[:5000]}".casefold()
    score = min(5, len(content) // 2_000)
    score += min(5, sum(marker in text for marker in _MAGIC_MARKERS))
    if candidate.doi:
        score += 7
    if host in _SCHOLARLY_HOSTS:
        score += 6
    if host in _TRUSTED_PRACTITIONER_HOSTS:
        score += 6
    if any(marker in text for marker in ("interview", "transcript", "essay", "theory")):
        score += 2
    if any(marker in text for marker in ("add to cart", "buy now", "sale price")):
        score -= 4
    return score


def _select(values: list[tuple[ResearchCandidate, str, int]], limit: int):
    return sorted(values, key=lambda item: (-item[2], item[0].title.casefold()))[:limit]


def _select_balanced(values: list[tuple[ResearchCandidate, str, int]], limit: int):
    by_domain = defaultdict(list)
    for item in values:
        by_domain[item[1]].append(item)
    quotas = {
        "classical_magic_theory": 10,
        "cognitive_science": 8,
        "expertise_training": 8,
        "magic_history": 30,
        "misdirection_theory": 20,
        "performance_theory": 14,
        "technique_knowledge": 25,
    }
    selected = []
    for domain, quota in quotas.items():
        selected.extend(_select(by_domain[domain], quota))
    selected_ids = {item[0].id for item in selected}
    remainder = [item for item in values if item[0].id not in selected_ids]
    selected.extend(_select(remainder, max(0, limit - len(selected))))
    return selected[:limit]


def _load_candidates(run: Path) -> list[ResearchCandidate]:
    values = []
    for name in ("academic-candidates.json", "practitioner-candidates.json"):
        values.extend(json.loads((run / "candidates" / name).read_text()))
    return TypeAdapter(list[ResearchCandidate]).validate_python(values)


def _decision(candidate: ResearchCandidate, reason: str, score: int):
    return {
        "candidate_id": candidate.id,
        "title": candidate.title,
        "url": candidate.url,
        "source_category": candidate.source_category.value,
        "reason": reason,
        "quality_score": score,
    }


def _host(url: str) -> str:
    return urlsplit(url).netloc.casefold().removeprefix("www.")


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
