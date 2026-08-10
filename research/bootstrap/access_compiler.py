"""Compile transient MCP access responses into exact, traceable source content.

The compiler is intentionally upstream of semantic extraction.  It rejects
failed/empty fetches and preserves both the requested and returned URL so an
Evidence Card can never be produced from a discovery snippet by accident.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import TypeAdapter

from research.models import ResearchCandidate


DEFAULT_RUN = Path("research/runs/bootstrap-002")
_EXA_HEADER = re.compile(r"(?m)^# (?P<title>[^\n]+)\nURL: (?P<url>https?://[^\n]+)\n")
_TAVILY_HEADER = re.compile(
    r"(?m)^Title: (?P<title>[^\n]+)\nURL: (?P<url>https?://[^\n]+)\n"
    r"Content:.*?\nRaw Content: "
)
_FAILED_TEXT = (
    "access denied",
    "captcha",
    "content: undefined\nraw content: undefined",
    "internal server error",
    "page not found",
    "robot check",
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    args = parser.parse_args()
    report = compile_access(args.run)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def compile_access(run: Path) -> dict[str, object]:
    candidates = _load_candidates(run)
    by_url = {_canonical_url(item.url): item for item in candidates}
    fetched: dict[str, dict[str, object]] = {}
    response_files = sorted((run / "discovery" / "access").glob("*.json"))
    for path in response_files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        blocks = [
            str(item.get("text") or "")
            for item in payload.get("content", [])
            if item.get("type") == "text"
        ]
        provider = "exa" if path.name.startswith("exa-") else "tavily"
        for block in blocks:
            parser = _parse_exa if provider == "exa" else _parse_tavily
            for item in parser(block):
                key = _canonical_url(item["url"])
                content = _clean(item["content"])
                previous = fetched.get(key)
                if previous is None or len(content) > len(str(previous["content"])):
                    fetched[key] = {
                        **item,
                        "content": content,
                        "provider": provider,
                        "response_file": str(path),
                    }

    sources: dict[str, dict[str, object]] = {}
    checks = []
    for key, candidate in by_url.items():
        item = fetched.get(key)
        content = str(item["content"]) if item else ""
        accepted, reason = _accept_content(content)
        checks.append(
            {
                "candidate_id": candidate.id,
                "title": candidate.title,
                "url": candidate.url,
                "source_category": candidate.source_category.value,
                "access_status": "readable_exact_content" if accepted else "rejected",
                "reason": reason,
                "content_characters": len(content),
                "provider": item.get("provider") if item else None,
                "returned_title": item.get("title") if item else None,
                "returned_url": item.get("url") if item else None,
            }
        )
        if accepted:
            sources[candidate.url] = {
                "candidate_id": candidate.id,
                "title": candidate.title,
                "content": content,
                "provider": item["provider"],
                "returned_url": item["url"],
                "retrieved_at": datetime.now(UTC).isoformat(),
            }

    output = run / "sources"
    output.mkdir(parents=True, exist_ok=True)
    _write(output / "source-content.json", {"sources": sources})
    _write(output / "access-checks.json", checks)
    status_counts = Counter(item["access_status"] for item in checks)
    category_access = Counter(
        item["source_category"]
        for item in checks
        if item["access_status"] == "readable_exact_content"
    )
    report = {
        "run_id": run.name,
        "compiled_at": datetime.now(UTC).isoformat(),
        "response_files": len(response_files),
        "candidates_checked": len(candidates),
        "readable_exact_content": status_counts["readable_exact_content"],
        "rejected_or_unavailable": status_counts["rejected"],
        "readable_by_discovery_channel": dict(sorted(category_access.items())),
        "state": "access_checked_not_extracted",
    }
    _write(run / "reports" / "access-report.json", report)
    return report


def _parse_exa(text: str) -> list[dict[str, str]]:
    matches = list(_EXA_HEADER.finditer(text))
    output = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        output.append(
            {
                "title": match.group("title").strip(),
                "url": match.group("url").strip(),
                "content": text[match.start() : end].strip(),
            }
        )
    return output


def _parse_tavily(text: str) -> list[dict[str, str]]:
    matches = list(_TAVILY_HEADER.finditer(text))
    output = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        output.append(
            {
                "title": match.group("title").strip(),
                "url": match.group("url").strip(),
                "content": text[match.end() : end].strip(),
            }
        )
    return output


def _accept_content(content: str) -> tuple[bool, str]:
    lowered = content.casefold()
    if not content:
        return False, "no_matching_fetch_result"
    if any(marker in lowered for marker in _FAILED_TEXT):
        return False, "fetch_returned_error_or_undefined_content"
    if len(content) < 700:
        return False, "content_too_short_for_traceable_claim_extraction"
    if len(re.findall(r"[a-zA-Z]{3,}", content)) < 80:
        return False, "insufficient_natural_language_content"
    return True, "exact_page_content_retrieved"


def _load_candidates(run: Path) -> list[ResearchCandidate]:
    values = []
    for name in ("academic-candidates.json", "practitioner-candidates.json"):
        values.extend(json.loads((run / "candidates" / name).read_text(encoding="utf-8")))
    return TypeAdapter(list[ResearchCandidate]).validate_python(values)


def _canonical_url(value: str) -> str:
    parts = urlsplit(value.strip())
    return urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold().removeprefix("www."),
            parts.path.rstrip("/"),
            parts.query,
            "",
        )
    )


def _clean(value: str) -> str:
    value = value.replace("\r\n", "\n").strip()
    value = re.sub(r"\n{4,}", "\n\n\n", value)
    return value


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
