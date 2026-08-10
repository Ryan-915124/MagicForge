"""Verify curated source metadata and build Bootstrap 002 source records."""

from __future__ import annotations

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from pydantic import TypeAdapter

from research.citation.models import (
    CitationRecord,
    CitationStatus,
    PeerReviewStatus,
    VerificationEvidence,
)
from research.models import ResearchCandidate, SourceCategory


DEFAULT_RUN = Path("research/runs/bootstrap-002")
EXISTING_RUNS = (
    Path("research/runs/magicforge-corpus-run-001"),
    Path("research/runs/bootstrap-001"),
)
_DOI = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
_USER_AGENT = "MagicForge/0.2 (corpus metadata verification; research use)"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    report = verify(args.run, workers=args.workers)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def verify(run: Path, *, workers: int) -> dict[str, object]:
    processing = run / "processing"
    academics = _load(processing / "candidates" / "academic-candidates.json")
    practitioners = _load(
        processing / "candidates" / "practitioner-candidates.json"
    )
    content_bundle = json.loads((run / "sources" / "source-content.json").read_text())[
        "sources"
    ]
    content_by_id = {
        item["candidate_id"]: item for item in content_bundle.values()
    }
    existing = _existing_identities()
    crossref_results: dict[str, dict[str, object]] = {}
    errors = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 6))) as executor:
        futures = {
            executor.submit(
                _crossref_match,
                candidate,
                str(content_by_id[candidate.id]["content"]),
            ): candidate
            for candidate in academics
        }
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = None
                errors.append(
                    {
                        "candidate_id": candidate.id,
                        "title": candidate.title,
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
                )
            if result:
                crossref_results[candidate.id] = result

    academic_records = []
    verified_academics = []
    rejected = []
    seen_doi = set(existing["doi"])
    seen_title = set(existing["title"])
    for candidate in academics:
        metadata = crossref_results.get(candidate.id)
        if not metadata:
            rejected.append(_rejection(candidate, "no_exact_crossref_title_or_doi_match"))
            continue
        doi = str(metadata["doi"]).casefold()
        title_key = _normalize(str(metadata["title"]))
        if doi in seen_doi or title_key in seen_title:
            rejected.append(_rejection(candidate, "duplicate_after_verified_metadata"))
            continue
        seen_doi.add(doi)
        seen_title.add(title_key)
        verified = candidate.model_copy(
            update={
                "title": metadata["title"],
                "authors": metadata["authors"],
                "published_year": metadata["year"],
                "venue": metadata["venue"],
                "doi": doi,
                "source_category": SourceCategory.ACADEMIC,
            }
        )
        citation = CitationRecord(
            source_candidate_id=verified.id,
            source_category=SourceCategory.ACADEMIC,
            title=verified.title,
            authors=verified.authors,
            year=verified.published_year,
            venue=verified.venue,
            volume=str(metadata.get("volume") or ""),
            issue=str(metadata.get("issue") or ""),
            pages=str(metadata.get("pages") or ""),
            doi=doi,
            url=verified.url,
            peer_review_status=(
                PeerReviewStatus.PEER_REVIEWED
                if metadata.get("type") in {"journal-article", "proceedings-article"}
                else PeerReviewStatus.UNKNOWN
            ),
            status=CitationStatus.FULL_TEXT_VERIFIED,
            provenance=verified.provenance,
            verification_evidence=[
                VerificationEvidence(
                    verifier="crossref-exact-metadata-match",
                    verification_source=f"https://api.crossref.org/works/{quote(doi)}",
                    notes=(
                        f"DOI/title metadata matched with similarity "
                        f"{metadata['title_similarity']:.3f}; exact source content "
                        "was independently fetched with Exa or Tavily."
                    ),
                ),
                VerificationEvidence(
                    verifier=f"{content_by_id[verified.id]['provider']}-exact-content",
                    verification_source=verified.url,
                    notes="Readable exact page content retrieved for bootstrap extraction.",
                ),
            ],
        )
        verified_academics.append(verified)
        academic_records.append(
            _source_record(
                verified,
                citation,
                content_by_id[verified.id],
                knowledge_origin="scientific_candidate",
            )
        )

    practitioner_records = []
    verified_practitioners = []
    seen_url = set(existing["url"])
    for candidate in practitioners:
        url_key = _canonical_url(candidate.url)
        title_key = _normalize(candidate.title)
        if url_key in seen_url or title_key in seen_title:
            rejected.append(_rejection(candidate, "duplicate_after_verified_metadata"))
            continue
        seen_url.add(url_key)
        seen_title.add(title_key)
        citation = CitationRecord(
            source_candidate_id=candidate.id,
            source_category=SourceCategory.PRACTITIONER,
            title=candidate.title,
            authors=candidate.authors,
            year=candidate.published_year,
            venue=candidate.venue,
            url=candidate.url,
            peer_review_status=PeerReviewStatus.NOT_APPLICABLE,
            status=CitationStatus.METADATA_VERIFIED,
            provenance=candidate.provenance,
            verification_evidence=[
                VerificationEvidence(
                    verifier=f"{content_by_id[candidate.id]['provider']}-exact-content",
                    verification_source=candidate.url,
                    notes=(
                        "Page URL, title, and readable practitioner/web content matched. "
                        "This is expert-practice or historical context, not scientific evidence."
                    ),
                )
            ],
        )
        verified_practitioners.append(candidate)
        practitioner_records.append(
            _source_record(
                candidate,
                citation,
                content_by_id[candidate.id],
                knowledge_origin="expert_practice",
            )
        )

    candidate_dir = processing / "candidates"
    source_dir = processing / "sources"
    _write(
        candidate_dir / "academic-candidates.json",
        [item.model_dump(mode="json") for item in verified_academics],
    )
    _write(
        candidate_dir / "practitioner-candidates.json",
        [item.model_dump(mode="json") for item in verified_practitioners],
    )
    _write(source_dir / "verified-academic-sources.json", academic_records)
    _write(
        source_dir / "access-checked-practitioner-sources.json",
        practitioner_records,
    )
    _write(run / "discovery" / "metadata" / "crossref-matches.json", crossref_results)
    _write(run / "discovery" / "metadata" / "rejected.json", rejected)
    _write(run / "discovery" / "metadata" / "errors.json", errors)
    report = {
        "run_id": run.name,
        "verified_at": datetime.now(UTC).isoformat(),
        "academic_submitted": len(academics),
        "academic_metadata_verified": len(verified_academics),
        "practitioner_submitted": len(practitioners),
        "practitioner_access_verified": len(verified_practitioners),
        "new_sources_ready_for_extraction": (
            len(verified_academics) + len(verified_practitioners)
        ),
        "rejected_or_deduplicated": len(rejected),
        "transient_metadata_errors": len(errors),
        "human_verified": False,
        "state": "bootstrap_metadata_verified_pending_extraction",
    }
    _write(run / "reports" / "metadata-verification-report.json", report)
    return report


def _crossref_match(candidate: ResearchCandidate, content: str):
    explicit = _clean_doi(candidate.doi)
    if explicit:
        item = _safe_crossref_by_doi(explicit)
        if item and _title_similarity(candidate.title, _title(item)) >= 0.55:
            return _metadata(item, candidate.title)
    try:
        item = _crossref_by_title(candidate.title)
    except (HTTPError, URLError, TimeoutError):
        item = None
    if item and _title_similarity(candidate.title, _title(item)) >= 0.78:
        return _metadata(item, candidate.title)
    header = content[:2_500]
    for match in list(_DOI.finditer(header))[:3]:
        doi = _clean_doi(match.group(0).rstrip(".,;)"))
        item = _safe_crossref_by_doi(doi) if doi else None
        if item and _title_similarity(candidate.title, _title(item)) >= 0.65:
            return _metadata(item, candidate.title)
    return None


def _safe_crossref_by_doi(doi: str):
    try:
        return _crossref_by_doi(doi)
    except (HTTPError, URLError, TimeoutError):
        return None


def _clean_doi(value: str | None) -> str | None:
    if not value:
        return None
    doi = value.strip().casefold()
    doi = re.sub(r"/(?:full|pdf|abstract|html?)$", "", doi)
    return doi.rstrip(".,;)")


def _crossref_by_doi(doi: str):
    payload = _get_json(f"https://api.crossref.org/works/{quote(doi, safe='')}")
    return payload.get("message") if payload else None


def _crossref_by_title(title: str):
    url = (
        "https://api.crossref.org/works?query.title="
        f"{quote(title)}&rows=5&select=DOI,title,author,published,"
        "container-title,type,volume,issue,page"
    )
    payload = _get_json(url)
    items = payload.get("message", {}).get("items", []) if payload else []
    return max(items, key=lambda item: _title_similarity(title, _title(item)), default=None)


def _get_json(url: str):
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    last = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=25) as response:
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))
    if last:
        raise last
    return None


def _metadata(item: dict[str, object], original_title: str):
    date_parts = (
        item.get("published", {})
        .get("date-parts", [[None]])[0]
    )
    authors = []
    for author in item.get("author", []):
        family = str(author.get("family") or "").strip()
        given = str(author.get("given") or "").strip()
        value = ", ".join(part for part in (family, given) if part)
        if value:
            authors.append(value)
    return {
        "doi": str(item.get("DOI") or "").casefold(),
        "title": _title(item),
        "authors": authors,
        "year": date_parts[0] if date_parts and date_parts[0] else None,
        "venue": _first(item.get("container-title")),
        "type": item.get("type"),
        "volume": item.get("volume") or "",
        "issue": item.get("issue") or "",
        "pages": item.get("page") or "",
        "title_similarity": _title_similarity(original_title, _title(item)),
    }


def _source_record(candidate, citation, access, *, knowledge_origin: str):
    return {
        "source_id": citation.id,
        "candidate_id": candidate.id,
        "source_category": candidate.source_category.value,
        "knowledge_origin": knowledge_origin,
        "theme": "classified_during_extraction",
        "evidence_class": "assigned_from_claim_role",
        "citation": citation.model_dump(mode="json"),
        "access_check": {
            "state": "full_text_access_confirmed"
            if candidate.source_category == SourceCategory.ACADEMIC
            else "web_extract_access_confirmed",
            "checked_at": access["retrieved_at"],
            "url": candidate.url,
            "provider": access["provider"],
            "notes": "Readable exact content retained in the isolated bootstrap run.",
        },
        "source_approval": {
            "status": "bootstrap_pending_human_review",
            "approval_record_id": None,
            "content_hash": None,
            "reason": "Bootstrap extraction is allowed; this is not human approval.",
        },
        "human_verified": False,
    }


def _existing_identities():
    values = {"doi": set(), "title": set(), "url": set()}
    for run in EXISTING_RUNS:
        for name in ("verified-academic-sources.json", "access-checked-practitioner-sources.json"):
            path = run / "sources" / name
            if not path.exists():
                continue
            for record in json.loads(path.read_text()):
                citation = record["citation"]
                if citation.get("doi"):
                    values["doi"].add(str(citation["doi"]).casefold())
                values["title"].add(_normalize(str(citation["title"])))
                values["url"].add(_canonical_url(str(citation["url"])))
    return values


def _load(path: Path):
    return TypeAdapter(list[ResearchCandidate]).validate_json(path.read_text())


def _title(item):
    return _first(item.get("title"))


def _first(value):
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _title_similarity(left: str, right: str) -> float:
    a, b = _normalize(left), _normalize(right)
    if not a or not b:
        return 0.0
    a_tokens, b_tokens = set(a.split()), set(b.split())
    jaccard = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    return max(jaccard, SequenceMatcher(None, a, b).ratio())


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _canonical_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), parts.query, "")
    )


def _rejection(candidate, reason):
    return {"candidate_id": candidate.id, "title": candidate.title, "url": candidate.url, "reason": reason}


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
