"""Tests for reusable, offline acquisition staging."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest

from research.acquisition.generic_staging import (
    main,
    stage_generic_acquisition,
    verify_generic_staging,
)
from research.acquisition.staging import AcquisitionStagingError
from research.review.workflow_models import SourceVersionCommand


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _academic_rows() -> list[dict]:
    base = {
        "title": "Attention and theatrical magic",
        "authors": ["Ada Researcher"],
        "published_year": 2025,
        "venue": "Journal of Conjuring Cognition",
        "doi": "10.1234/magic.2025.7",
        "snippet": "A controlled study of attention during a theatrical magic performance.",
        "provider": "exa",
        "source_category": "academic",
        "source_type": "journal_article",
        "knowledge_origin": "scientific_evidence",
        "peer_review_status": "peer_reviewed",
        "query": "magic attention experiment",
        "retrieved_at": "2026-08-09T10:00:00+00:00",
        "rank": 1,
    }
    return [
        {**base, "url": "https://example.org/paper?utm_source=test"},
        {
            **base,
            "url": "https://doi.org/10.1234/magic.2025.7",
            "provider": "tavily",
            "tool_name": "tavily_search",
            "query": "practitioner application of attention research",
            "rank": 2,
        },
    ]


def _unresolved_academic() -> dict:
    return {
        "title": "A preliminary surprise manuscript",
        "url": "https://papers.example.net/surprise",
        "authors": ["B. Scholar"],
        "snippet": "A preliminary research manuscript about surprise in magic.",
        "provider": "exa",
        "source_category": "academic",
        "knowledge_origin": "scientific_evidence",
        "query": "magic surprise preprint",
        "retrieved_at": "2026-08-09T10:01:00+00:00",
        "rank": 3,
    }


def _practitioner() -> dict:
    return {
        "title": "Notes on directing attention",
        "url": "https://magician.example.com/notes/attention/",
        "authors": ["Expert Magician"],
        "snippet": "A practitioner essay on directing attention in close-up magic.",
        "provider": "tavily",
        "source_category": "practitioner",
        "source_type": "web_article",
        "knowledge_origin": "expert_practice",
        "sensitive_information_level": "controlled",
        "query": "magician attention essay",
        "retrieved_at": "2026-08-09T10:02:00+00:00",
        "rank": 1,
    }


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    discovery = _write(
        tmp_path / "discovery.json",
        {"results": [*_academic_rows(), _unresolved_academic(), _practitioner()]},
    )
    content = _write(
        tmp_path / "content.json",
        {
            "sources": [
                {
                    "requested_url": "https://example.org/paper",
                    "returned_url": "https://example.org/paper",
                    "title": "Attention and theatrical magic",
                    "content": "Exact academic paper text. " * 20,
                    "content_access": "full_text",
                    "provider": "exa",
                    "retrieved_at": "2026-08-09T11:00:00+00:00",
                },
                {
                    "requested_url": "https://papers.example.net/surprise",
                    "returned_url": "https://papers.example.net/surprise",
                    "title": "A preliminary surprise manuscript",
                    "content": "Exact but unclassified academic manuscript. " * 20,
                    "content_access": "web_extract",
                    "provider": "exa",
                    "retrieved_at": "2026-08-09T11:01:00+00:00",
                },
                {
                    "requested_url": "https://magician.example.com/notes/attention",
                    "returned_url": "https://magician.example.com/notes/attention/",
                    "title": "Notes on directing attention",
                    "content": "Exact practitioner essay text. " * 20,
                    "content_access": "web_extract",
                    "provider": "tavily",
                    "retrieved_at": "2026-08-09T11:02:00+00:00",
                },
            ]
        },
    )
    return discovery, content, tmp_path / "staged"


def test_staging_deduplicates_and_emits_content_addressed_dry_run(
    tmp_path: Path,
) -> None:
    discovery, content, output = _fixture(tmp_path)

    report = stage_generic_acquisition([discovery], content, output)

    assert report["status"] == "dry_run"
    assert report["summary"] == {
        "discovery_candidates": 3,
        "retained_candidates": 3,
        "exact_content_matched": 3,
        "source_payloads_prepared": 2,
        "blocked_candidates": 1,
    }
    ledger_path = output / report["ledger_file"]
    ledger = json.loads(ledger_path.read_text())
    assert ledger["ledger_sha256"] == report["ledger_sha256"]
    assert ledger["deduplication"]["duplicate_discovery_rows"] == 1
    assert ledger["constraints"]["api_submission"] is False
    assert ledger["constraints"]["approval_performed"] is False
    assert ledger["constraints"]["qdrant_write"] is False
    assert "Exact academic paper text" not in ledger_path.read_text()

    academic = next(entry for entry in ledger["entries"] if entry["doi"])
    assert academic["identity"] == {
        "kind": "doi",
        "canonical_value": "10.1234/magic.2025.7",
        "key": "doi:10.1234/magic.2025.7",
    }
    assert len(academic["provenance"]) == 2
    assert academic["sensitive_information_level"] == "controlled"
    payload_path = output / academic["source_submission"]["payload_file"]
    payload_raw = payload_path.read_bytes()
    assert hashlib.sha256(payload_raw).hexdigest() == academic["source_submission"][
        "payload_sha256"
    ]
    wrapper = json.loads(payload_raw)
    command = SourceVersionCommand.model_validate(wrapper["payload"])
    assert command.requested_extraction_permission.value == "none"
    assert command.requested_storage_permission.value == "none"
    assert command.citation_verification == []
    assert command.knowledge_origin.value == "scientific_evidence"
    assert command.content.startswith("Exact academic paper text")
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(payload_path.stat().st_mode) == 0o600

    unresolved = next(
        entry for entry in ledger["entries"] if entry["source_type"] is None
    )
    assert unresolved["source_submission"]["blocked_reasons"] == [
        "source_type_requires_review"
    ]
    assert unresolved["source_submission"]["payload_file"] is None
    assert verify_generic_staging(output, report["ledger_sha256"]) == {
        "ledger_verified": 1,
        "source_payloads_verified": 2,
    }


def test_staging_is_byte_stable_and_existing_corpus_dedupe_is_explicit(
    tmp_path: Path,
) -> None:
    discovery, content, output = _fixture(tmp_path)
    existing = _write(
        tmp_path / "existing.json",
        [
            {
                **_practitioner(),
                "id": "",
                "content_access": "search_snippet",
                "provenance": [
                    {
                        "provider": "tavily",
                        "tool_name": "tavily_search",
                        "query": "existing-corpus",
                        "retrieved_at": "2026-08-01T00:00:00+00:00",
                        "rank": 1,
                    }
                ],
            }
        ],
    )

    first = stage_generic_acquisition(
        [discovery], content, output, existing_candidate_paths=[existing]
    )
    second = stage_generic_acquisition(
        [discovery], content, output, existing_candidate_paths=[existing]
    )

    assert first["ledger_sha256"] == second["ledger_sha256"]
    ledger = json.loads((output / first["ledger_file"]).read_text())
    assert ledger["summary"]["retained_candidates"] == 2
    assert len(ledger["deduplication"]["duplicates_against_existing"]) == 1


def test_discovery_snippet_never_becomes_source_content(tmp_path: Path) -> None:
    discovery = _write(
        tmp_path / "discovery.json",
        {
            "results": [
                {
                    **_practitioner(),
                    "snippet": "THIS MUST NEVER BECOME EXACT SOURCE CONTENT",
                }
            ]
        },
    )
    content = _write(tmp_path / "content.json", {"sources": []})

    report = stage_generic_acquisition(
        [discovery], content, tmp_path / "staged"
    )
    ledger = json.loads(
        ((tmp_path / "staged") / report["ledger_file"]).read_text()
    )

    assert ledger["summary"]["source_payloads_prepared"] == 0
    assert ledger["entries"][0]["source_submission"]["blocked_reasons"] == [
        "exact_content_missing"
    ]
    assert "THIS MUST NEVER" not in json.dumps(ledger)


def test_tampered_payload_fails_integrity_verification(tmp_path: Path) -> None:
    discovery, content, output = _fixture(tmp_path)
    report = stage_generic_acquisition([discovery], content, output)
    ledger = json.loads((output / report["ledger_file"]).read_text())
    prepared = next(
        entry
        for entry in ledger["entries"]
        if entry["source_submission"]["status"] == "prepared"
    )
    payload_path = output / prepared["source_submission"]["payload_file"]
    payload_path.write_bytes(payload_path.read_bytes() + b"\n")

    with pytest.raises(AcquisitionStagingError) as caught:
        verify_generic_staging(output, report["ledger_sha256"])
    assert caught.value.code == "source_payload_hash_mismatch"


def test_cli_has_no_submit_mode_and_defaults_to_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    discovery, content, output = _fixture(tmp_path)

    assert (
        main(
            [
                "--discovery",
                str(discovery),
                "--content-bundle",
                str(content),
                "--output-dir",
                str(output),
                "--json",
            ]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "dry_run"
    assert printed["safety"] == {
        "network_called": False,
        "database_modified": False,
        "approval_created": False,
        "qdrant_modified": False,
    }


def test_naive_provenance_timestamp_is_rejected(tmp_path: Path) -> None:
    row = _practitioner()
    row["retrieved_at"] = "2026-08-09T10:00:00"
    discovery = _write(tmp_path / "discovery.json", {"results": [row]})
    content = _write(tmp_path / "content.json", {"sources": []})

    with pytest.raises(AcquisitionStagingError) as caught:
        stage_generic_acquisition([discovery], content, tmp_path / "staged")
    assert caught.value.code == "provenance_timestamp_naive"


def test_discovery_embedded_content_is_rejected(tmp_path: Path) -> None:
    row = _practitioner()
    row["content"] = "A search result must not smuggle exact Source content."
    discovery = _write(tmp_path / "discovery.json", {"results": [row]})
    content = _write(tmp_path / "content.json", {"sources": []})

    with pytest.raises(AcquisitionStagingError) as caught:
        stage_generic_acquisition([discovery], content, tmp_path / "staged")
    assert caught.value.code == "discovery_content_forbidden"


def test_invalid_legacy_existing_url_still_deduplicates_by_title(
    tmp_path: Path,
) -> None:
    row = _practitioner()
    discovery = _write(tmp_path / "discovery.json", {"results": [row]})
    content = _write(
        tmp_path / "content.json",
        {
            "sources": [
                {
                    "requested_url": row["url"],
                    "returned_url": row["url"],
                    "title": row["title"],
                    "content": "Exact practitioner essay text. " * 20,
                    "content_access": "web_extract",
                    "provider": "tavily",
                    "retrieved_at": "2026-08-09T12:00:00+00:00",
                }
            ]
        },
    )
    existing = _write(
        tmp_path / "legacy-existing.json",
        [
            {
                "title": row["title"],
                "url": "https:///goto?url=opaque-provider-token",
                "authors": row["authors"],
                "source_category": "practitioner",
            }
        ],
    )

    report = stage_generic_acquisition(
        [discovery],
        content,
        tmp_path / "staged",
        existing_candidate_paths=[existing],
    )

    assert report["summary"] == {
        "discovery_candidates": 1,
        "retained_candidates": 0,
        "exact_content_matched": 0,
        "source_payloads_prepared": 0,
        "blocked_candidates": 0,
    }


def test_unverified_redirect_and_wrong_title_block_source_payload(
    tmp_path: Path,
) -> None:
    row = _practitioner()
    discovery = _write(tmp_path / "discovery.json", {"results": [row]})
    content = _write(
        tmp_path / "content.json",
        {
            "sources": [
                {
                    "requested_url": row["url"],
                    "returned_url": "https://unrelated.example.net/page",
                    "title": "An unrelated shopping page",
                    "content": "Wrong exact content. " * 50,
                    "content_access": "web_extract",
                    "provider": "tavily",
                    "retrieved_at": "2026-08-09T12:00:00+00:00",
                }
            ]
        },
    )

    report = stage_generic_acquisition([discovery], content, tmp_path / "staged")
    ledger = json.loads(
        ((tmp_path / "staged") / report["ledger_file"]).read_text()
    )

    assert ledger["summary"]["source_payloads_prepared"] == 0
    assert ledger["entries"][0]["source_submission"]["blocked_reasons"] == [
        "returned_url_requires_verification",
        "returned_title_mismatch",
    ]


def test_existing_title_dedupes_across_punctuation_and_missing_author(
    tmp_path: Path,
) -> None:
    row = {
        **_practitioner(),
        "title": "Fully Booked | The Structural Conception of Magic",
        "authors": ["Harapan Ong"],
        "url": "https://new.example.com/fully-booked",
    }
    discovery = _write(tmp_path / "discovery.json", {"results": [row]})
    content = _write(tmp_path / "content.json", {"sources": []})
    existing = _write(
        tmp_path / "existing.json",
        [
            {
                "title": "Fully Booked: The Structural Conception of Magic",
                "url": "https://old.example.com/fully-booked",
                "authors": [],
                "source_category": "practitioner",
            }
        ],
    )

    report = stage_generic_acquisition(
        [discovery],
        content,
        tmp_path / "staged",
        existing_candidate_paths=[existing],
    )

    assert report["summary"]["retained_candidates"] == 0


def test_obvious_mojibake_blocks_source_payload(tmp_path: Path) -> None:
    row = _practitioner()
    discovery = _write(tmp_path / "discovery.json", {"results": [row]})
    content = _write(
        tmp_path / "content.json",
        {
            "sources": [
                {
                    "requested_url": row["url"],
                    "returned_url": row["url"],
                    "title": row["title"],
                    "content": ("Broken quotation \u00e2\u20ac\u0153text\u00e2\u20ac\u009d. " * 20),
                    "content_access": "web_extract",
                    "provider": "tavily",
                    "retrieved_at": "2026-08-09T12:00:00+00:00",
                }
            ]
        },
    )

    report = stage_generic_acquisition([discovery], content, tmp_path / "staged")
    ledger = json.loads(
        ((tmp_path / "staged") / report["ledger_file"]).read_text()
    )

    assert report["summary"]["source_payloads_prepared"] == 0
    assert ledger["entries"][0]["source_submission"]["blocked_reasons"] == [
        "exact_content_encoding_corrupt"
    ]
