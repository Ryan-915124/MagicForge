"""Generate a locked, content-safe Source-acquisition run for public tests."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from uuid import NAMESPACE_URL, uuid5

import research.acquisition.staging as staging
import research.acquisition.tavily_results as tavily_results


@dataclass(frozen=True, slots=True)
class SyntheticAcquisitionRun:
    root: Path
    candidate_ids: frozenset[str]
    input_lock: tuple[dict[str, Any], ...]
    unresolved_candidate_id: str


@dataclass(frozen=True, slots=True)
class SyntheticOfficialAcquisition:
    run_root: Path
    payload_directory: Path
    results_file: Path
    manifest_sha256: str
    results_sha256: str


def write_synthetic_source_run(root: Path) -> SyntheticAcquisitionRun:
    """Write 44 invented candidates with the same safety shape as Run 001."""

    root = Path(root).resolve()
    academic_candidates: list[dict[str, Any]] = []
    practitioner_candidates: list[dict[str, Any]] = []
    academic_sources: list[dict[str, Any]] = []
    practitioner_sources: list[dict[str, Any]] = []
    candidate_ids: set[str] = set()

    for index in range(32):
        number = index + 1
        doi = f"10.9999/magicforge.synthetic-academic-{number:02d}"
        url = f"https://doi.org/{doi}"
        title = f"Synthetic Attention Study {number:02d}"
        candidate_id = str(
            uuid5(NAMESPACE_URL, f"magicforge:research:{doi}")
        )
        provenance = [_provenance(f"synthetic academic query {number:02d}")]
        citation = {
            "title": title,
            "authors": ["MagicForge Public Test Author"],
            "year": 2025,
            "venue": "Synthetic Test Journal",
            "volume": "1",
            "issue": "1",
            "pages": f"{number}-{number + 1}",
            "doi": doi,
            "url": url,
            "peer_review_status": "unknown",
            "provenance": provenance,
        }
        academic_candidates.append(
            {
                "id": candidate_id,
                "title": title,
                "url": url,
                "authors": citation["authors"],
                "published_year": citation["year"],
                "venue": citation["venue"],
                "doi": doi,
                "snippet": "",
                "content": None,
                "content_access": "search_snippet",
                "source_category": "academic",
                "provenance": provenance,
            }
        )
        ready = index < 18
        academic_sources.append(
            {
                "candidate_id": candidate_id,
                "source_id": str(
                    uuid5(NAMESPACE_URL, f"magicforge:synthetic-source:{candidate_id}")
                ),
                "source_category": "academic",
                "citation": citation,
                "source_role": "journal_article",
                "access_check": {
                    "state": (
                        "web_extract_access_confirmed"
                        if ready
                        else "access_unresolved"
                    ),
                    "url": (
                        f"https://doi.org/{doi}"
                        if ready
                        else ""
                    ),
                    "checked_at": "2026-01-01T00:00:00Z",
                    "notes": "Self-authored public test fixture.",
                },
                "semantic_scholar": {},
                "source_approval": {"status": "pending"},
            }
        )
        candidate_ids.add(candidate_id)

    unresolved_candidate_id = ""
    for index in range(12):
        number = index + 1
        url = f"https://example.test/practitioner/{number:02d}"
        title = f"Synthetic Performance Essay {number:02d}"
        candidate_id = str(
            uuid5(NAMESPACE_URL, f"magicforge:research:{url}")
        )
        provenance = [_provenance(f"synthetic practitioner query {number:02d}")]
        citation = {
            "title": title,
            "authors": ["MagicForge Public Test Author"],
            "year": 2025,
            "venue": "Synthetic Practitioner Archive",
            "volume": "",
            "issue": "",
            "pages": "",
            "doi": None,
            "url": url,
            "peer_review_status": "unknown",
            "provenance": provenance,
        }
        practitioner_candidates.append(
            {
                "id": candidate_id,
                "title": title,
                "url": url,
                "authors": citation["authors"],
                "published_year": citation["year"],
                "venue": citation["venue"],
                "doi": None,
                "snippet": "",
                "content": None,
                "content_access": "search_snippet",
                "source_category": "practitioner",
                "provenance": provenance,
            }
        )
        unresolved = index == 11
        if unresolved:
            unresolved_candidate_id = candidate_id
        practitioner_sources.append(
            {
                "candidate_id": candidate_id,
                "source_id": str(
                    uuid5(NAMESPACE_URL, f"magicforge:synthetic-source:{candidate_id}")
                ),
                "source_category": "practitioner",
                "citation": citation,
                "source_role": (
                    "unofficial_interview_archive"
                    if unresolved
                    else "practitioner_essay"
                ),
                "access_check": {
                    "state": "web_extract_access_confirmed",
                    "url": url,
                    "checked_at": "2026-01-01T00:00:00Z",
                    "notes": "Self-authored public test fixture.",
                },
                "source_approval": {"status": "pending"},
            }
        )
        candidate_ids.add(candidate_id)

    files: tuple[tuple[str, str, list[dict[str, Any]]], ...] = (
        (
            "academic_candidates",
            "candidates/academic-candidates.json",
            academic_candidates,
        ),
        (
            "practitioner_candidates",
            "candidates/practitioner-candidates.json",
            practitioner_candidates,
        ),
        (
            "verified_academic_sources",
            "sources/verified-academic-sources.json",
            academic_sources,
        ),
        (
            "access_checked_practitioner_sources",
            "sources/access-checked-practitioner-sources.json",
            practitioner_sources,
        ),
    )
    input_lock: list[dict[str, Any]] = []
    for role, relative, value in files:
        path = root / relative
        _write_json(path, value)
        input_lock.append(
            {
                "role": role,
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "record_count": len(value),
            }
        )

    if not unresolved_candidate_id:  # pragma: no cover - construction invariant
        raise AssertionError("synthetic run requires one unresolved Source type")
    return SyntheticAcquisitionRun(
        root=root,
        candidate_ids=frozenset(candidate_ids),
        input_lock=tuple(input_lock),
        unresolved_candidate_id=unresolved_candidate_id,
    )


def write_synthetic_official_acquisition(
    source_run: SyntheticAcquisitionRun,
    run_root: Path,
) -> SyntheticOfficialAcquisition:
    """Stage the full 30-result/29-payload bundle from invented provider data."""

    run_root = Path(run_root).resolve()
    manifest_path = run_root / "acquisition-manifest.json"
    input_dir = run_root / "saved-exports"
    with temporary_acquisition_locks(source_run):
        manifest = staging.write_acquisition_manifest(source_run.root, manifest_path)
        write_synthetic_tavily_exports(manifest, input_dir)
        results = tavily_results.process_saved_tavily_exports(
            manifest_path,
            input_dir,
            run_root,
        )
    return SyntheticOfficialAcquisition(
        run_root=run_root,
        payload_directory=run_root / "source_payloads",
        results_file=run_root / "acquisition-results.json",
        manifest_sha256=manifest["manifest_sha256"],
        results_sha256=results["results_sha256"],
    )


def write_synthetic_tavily_exports(
    manifest: dict[str, Any],
    input_dir: Path,
) -> None:
    entries = manifest["entries"]
    practitioner = sorted(
        (
            entry
            for entry in entries
            if entry["acquisition"]["status"] == "ready"
            and entry["source_category"] == "practitioner"
        ),
        key=lambda entry: entry["candidate_id"],
    )
    academic = sorted(
        (
            entry
            for entry in entries
            if entry["acquisition"]["status"] == "ready"
            and entry["source_category"] == "academic"
        ),
        key=lambda entry: entry["candidate_id"],
    )
    fallback = academic[-5:]
    basic = academic[:-5]
    _write_json(
        Path(input_dir) / "practitioner-basic.json",
        {
            "request_id": "synthetic-practitioner-basic",
            "response_time": 0.1,
            "results": [_tavily_result(entry) for entry in practitioner],
            "failed_results": [],
        },
    )
    _write_json(
        Path(input_dir) / "academic-basic.json",
        {
            "request_id": "synthetic-academic-basic",
            "response_time": 0.2,
            "results": [_tavily_result(entry) for entry in basic],
            "failed_results": [
                {"url": _selected_locator(entry)["url"], "error": "fixture fallback"}
                for entry in fallback
            ],
        },
    )
    _write_json(
        Path(input_dir) / "academic-advanced-fallback.json",
        {
            "request_id": "synthetic-academic-advanced",
            "response_time": 0.3,
            "results": [_tavily_result(entry) for entry in fallback],
            "failed_results": [],
        },
    )


@contextmanager
def temporary_acquisition_locks(
    source_run: SyntheticAcquisitionRun,
) -> Iterator[None]:
    """Temporarily bind production lock consumers to one synthetic test run."""

    changes = (
        (staging, "EXPECTED_CANDIDATE_IDS", source_run.candidate_ids),
        (staging, "EXPECTED_INPUT_LOCK", source_run.input_lock),
        (tavily_results, "EXPECTED_CANDIDATE_IDS", source_run.candidate_ids),
        (tavily_results, "EXPECTED_INPUT_LOCK", source_run.input_lock),
        (
            tavily_results,
            "UNRESOLVED_SOURCE_TYPE_CANDIDATE_ID",
            source_run.unresolved_candidate_id,
        ),
    )
    previous = [(module, name, getattr(module, name)) for module, name, _ in changes]
    try:
        for module, name, value in changes:
            setattr(module, name, value)
        yield
    finally:
        for module, name, value in previous:
            setattr(module, name, value)


def _provenance(query: str) -> dict[str, Any]:
    return {
        "provider": "tavily",
        "tool_name": "synthetic_offline_fixture",
        "query": query,
        "retrieved_at": "2026-01-01T00:00:00Z",
        "provider_result_id": None,
        "rank": 1,
    }


def _selected_locator(entry: dict[str, Any]) -> dict[str, Any]:
    kind = (
        "publisher_or_repository_html"
        if entry["source_category"] == "academic"
        else "practitioner_web_page"
    )
    return next(
        locator
        for locator in entry["acquisition"]["content_locators"]
        if locator["kind"] == kind
    )


def _tavily_result(entry: dict[str, Any]) -> dict[str, Any]:
    title = entry["citation"]["title"]
    padding = "Synthetic research and performance context. " * 140
    if entry["source_category"] == "academic":
        content = (
            f"# {title}\n\nDOI: {entry['citation']['doi']}\n\n"
            "## Abstract\nThis invented study defines a test question.\n\n"
            "## Introduction\nSynthetic context is introduced.\n\n"
            "## Methods\nThe fixture describes a fictional method.\n\n"
            "## Results\nThe fixture reports invented results.\n\n"
            "## References\nNo external works are reproduced.\n\n"
            f"{padding}"
        )
    else:
        content = f"# {title}\n\n{padding}"
    return {
        "url": _selected_locator(entry)["url"],
        "title": title,
        "raw_content": content,
        "images": [],
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
