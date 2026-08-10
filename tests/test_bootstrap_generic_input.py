from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from knowledge.evidence import (
    ClaimRole,
    EvidenceClass,
    KnowledgeOrigin,
    MagicDomain,
)
from knowledge.governance import MagicForgeMode
from research.acquisition.generic_staging import stage_generic_acquisition
from research.bootstrap.generic_input import (
    BootstrapInputError,
    execute_prepared_bootstrap,
    execute_prepared_bootstrap_batches,
    prepare_academic_bootstrap_input,
    prepare_bootstrap_input,
)
from research.bootstrap.runner import (
    SUPERSEDED_PREPARED_INPUT_LEDGERS,
    _apply_source_claim_policy,
    _load_source_claim_policies,
    _process_candidate,
    _verify_prepared_input_manifest,
)
from research.bootstrap.service import BootstrapSourceRegistrar
from research.citation.manager import CitationManager
from research.extraction.extractor import ResearchKnowledgeExtractor
from research.extraction.models import ExtractedClaim, ResearchExtractionResult
from research.models import (
    ContentAccess,
    DiscoveryProvider,
    ResearchCandidate,
    SearchProvenance,
    SourceCategory,
)


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _staged_fixture(tmp_path: Path) -> tuple[Path, str, Path, Path]:
    url = "https://magician.example.org/archive/attention"
    discovery = _write(
        tmp_path / "discovery.json",
        {
            "results": [
                {
                    "title": "Notes on directing attention",
                    "url": url,
                    "authors": ["Expert Magician"],
                    "published_year": 2024,
                    "venue": "Example Magic Archive",
                    "snippet": "A practitioner essay about attention and performance craft.",
                    "provider": "tavily",
                    "source_category": "practitioner",
                    "source_type": "web_article",
                    "knowledge_origin": "expert_practice",
                    "peer_review_status": "not_applicable",
                    "sensitive_information_level": "controlled",
                    "query": "magician attention performance essay",
                    "retrieved_at": "2026-08-09T10:00:00+00:00",
                    "rank": 1,
                }
            ]
        },
    )
    content_text = (
        "The performer recommends directing the audience's attention before a move. "
        "This is a personal account of performance practice. "
    ) * 20
    content = _write(
        tmp_path / "content.json",
        {
            "sources": [
                {
                    "requested_url": url,
                    "returned_url": url,
                    "title": "Notes on directing attention",
                    "content": content_text,
                    "content_access": "web_extract",
                    "provider": "tavily_extract",
                    "retrieved_at": "2026-08-09T10:05:00+00:00",
                }
            ]
        },
    )
    staging = tmp_path / "staging"
    staged = stage_generic_acquisition([discovery], content, staging)
    audit = _write(
        tmp_path / "strict-audit.json",
        {
            "schema_version": "magicforge-practitioner-exact-content-audit-0.1",
            "run_id": "test-acquisition",
            "lane": "practitioner_performance_history",
            "scope": {
                "records_reviewed": 1,
                "database_write": False,
                "glm_called": False,
                "approval_created": False,
                "qdrant_write": False,
            },
            "policy": {
                "knowledge_origin": "expert_practice",
                "peer_review_status": "not_applicable",
                "scientific_assertion_claim_role": "context_only",
                "scientific_assertion_evidence_class": "never_empirical",
                "source_level_pass_does_not_equal_claim_approval": True,
            },
            "records": [
                {
                    "url": url,
                    "title": "Notes on directing attention",
                    "authors": ["Expert Magician"],
                    "classification": "practitioner_theory",
                    "claim_roles_allowed": ["expert_opinion", "context_only"],
                    "disposition": "eligible_exact_content",
                    "verification": "Visible title, byline, and complete essay body match.",
                    "restriction": "Advice remains expert practice, not empirical evidence.",
                }
            ],
        },
    )
    return staging, staged["ledger_sha256"], audit, tmp_path / "bootstrap-003"


def _academic_staged_fixture(
    tmp_path: Path,
) -> tuple[Path, str, Path, Path, Path]:
    url = "https://pmc.example.org/articles/PMC12345"
    doi = "10.1234/example.corrected"
    title = "Corrected year paper: attention and memory"
    discovery = _write(
        tmp_path / "academic-discovery.json",
        {
            "results": [
                {
                    "title": title,
                    "url": url,
                    "authors": ["Research Author"],
                    "published_year": 2017,
                    "venue": "Journal of Example Cognition",
                    "doi": doi,
                    "snippet": "A peer-reviewed study of attention and memory.",
                    "provider": "exa",
                    "source_category": "academic",
                    "source_type": "journal_article",
                    "knowledge_origin": "scientific_evidence",
                    "peer_review_status": "peer_reviewed",
                    "sensitive_information_level": "public",
                    "query": "attention memory experiment",
                    "retrieved_at": "2026-08-09T10:00:00+00:00",
                    "rank": 1,
                }
            ]
        },
    )
    content_text = (
        "# Corrected year paper: attention and memory\n\n"
        "Methods: participants completed an attention task. "
        "Results: performance changed under the tested condition. "
        "Discussion: the result may depend on the sampled population. "
        "References: Example (2015).\n"
    ) * 12
    content = _write(
        tmp_path / "academic-content.json",
        {
            "sources": [
                {
                    "requested_url": url,
                    "returned_url": url,
                    "title": title,
                    "content": content_text,
                    "content_access": "web_extract",
                    "provider": "tavily_extract",
                    "retrieved_at": "2026-08-09T10:05:00+00:00",
                }
            ]
        },
    )
    staging = tmp_path / "academic-staging"
    staged = stage_generic_acquisition([discovery], content, staging)
    ledger_hash = staged["ledger_sha256"]
    ledger = json.loads((staging / "ledgers" / f"{ledger_hash}.json").read_text())
    exact_hash = ledger["entries"][0]["exact_content"]["content_sha256"]
    old_ledger_hash = "1" * 64
    full_audit = _write(
        tmp_path / "academic-full-text-audit.json",
        {
            "schema_version": "magicforge-academic-full-text-audit-0.1",
            "summary": {"accepted_full_text": 1},
            "safety": {
                "database_modified": False,
                "glm_called": False,
                "approval_created": False,
                "qdrant_modified": False,
            },
            "bibliographic_corrections": [
                {
                    "doi": doi,
                    "field": "published_year",
                    "superseded_value": 2016,
                    "authoritative_value": 2017,
                    "exact_full_text_changed": False,
                    "superseded_ledger": old_ledger_hash,
                    "authoritative_ledger": ledger_hash,
                }
            ],
            "accepted": [
                {
                    "title": title,
                    "url": f"{url}/",
                    "pmcid": "PMC12345",
                    "doi": doi,
                    "peer_review_status": "peer_reviewed",
                    "full_text": {
                        "characters": len(content_text),
                        "article_heading": True,
                        "reference_mentions": 1,
                        "complete_ending_preserved": True,
                        "truncation_detected": False,
                    },
                    "verdict": "accepted_pending_human_source_review",
                }
            ],
        },
    )
    independent_audit = _write(
        tmp_path / "academic-independent-audit.json",
        {
            "schema_version": "magicforge-academic-independent-audit-0.1",
            "audited_ledger_sha256": ledger_hash,
            "ledger_history": {
                "authoritative": {
                    "ledger_sha256": ledger_hash,
                    "integrity": "pass",
                },
                "superseded": {"ledger_sha256": old_ledger_hash},
            },
            "scope": {
                "original_discovery_modified": False,
                "original_full_text_modified": False,
                "database_read_or_write": False,
                "glm_called": False,
                "approval_created": False,
                "qdrant_read_or_write": False,
            },
            "summary": {
                "ledger_integrity_pass": True,
                "payload_hashes_verified": 1,
                "complete_claim_extractable_full_text": 1,
                "crossref_doi_resolved": 1,
                "publication_year_matches": 1,
                "peer_reviewed_final_or_accepted_journal_records": 1,
                "knowledge_origin_boundary_pass": 1,
                "eligible_for_source_review": 1,
                "held_for_metadata_correction": 0,
            },
            "evidence_boundary_assessment": {
                "result": "pass",
                "knowledge_origin": "scientific_evidence",
                "source_category": "academic",
                "source_type": "journal_article",
                "peer_review_status": "peer_reviewed",
                "requested_extraction_permission": "none",
                "requested_storage_permission": "none",
            },
            "resolved_metadata_issue": {
                "status": "resolved_in_authoritative_ledger",
                "doi": doi,
                "superseded_staged_year": 2016,
                "authoritative_staged_year": 2017,
                "crossref_issued_year": 2017,
                "crossref_published_online_year": 2017,
                "exact_content_changed": False,
                "exact_content_sha256": exact_hash,
                "disposition": "resolved_eligible_for_source_review",
            },
            "records": [
                {
                    "doi": doi,
                    "pmcid": "PMC12345",
                    "title": title,
                    "content_bytes": len(content_text.encode("utf-8")),
                    "full_text": "pass",
                    "metadata": "pass_year_corrected_to_2017",
                    "deduplication": "unique",
                    "boundary": "pass",
                    "disposition": "eligible_for_source_review",
                }
            ],
            "overall_verdict": "pass_pending_human_source_review",
            "blocking_action": None,
        },
    )
    return (
        staging,
        ledger_hash,
        full_audit,
        independent_audit,
        tmp_path / "bootstrap-003",
    )


def test_prepare_generic_practitioner_input_is_offline_and_fail_closed(
    tmp_path: Path,
) -> None:
    staging, ledger_hash, audit, output = _staged_fixture(tmp_path)

    result = prepare_bootstrap_input(staging, ledger_hash, audit, output)

    assert result["status"] == "dry_run_prepared"
    assert result["glm_called"] is False
    assert result["qdrant_modified"] is False
    manifest = json.loads(Path(result["manifest_file"]).read_text())
    assert manifest["constraints"] == {
        "approval_created": False,
        "database_modified": False,
        "glm_called": False,
        "human_verified": False,
        "production_collection_modified": False,
        "qdrant_modified": False,
        "review_status_after_extraction": "bootstrap_generated",
    }
    input_root = Path(result["input_root"])
    candidates = json.loads(
        (input_root / "candidates" / "practitioner-candidates.json").read_text()
    )
    assert len(candidates) == 1
    assert candidates[0]["content"] is None
    assert candidates[0]["content_access"] == "search_snippet"
    source = json.loads(
        (
            input_root
            / "sources"
            / "access-checked-practitioner-sources.json"
        ).read_text()
    )[0]
    assert source["knowledge_origin"] == "expert_practice"
    assert source["source_approval"]["status"] == (
        "bootstrap_pending_human_review"
    )
    assert source["source_approval"]["human_verified"] is False
    policies = _load_source_claim_policies([input_root])
    policy = policies[candidates[0]["id"]]
    assert policy["allowed_claim_roles"] == ["expert_opinion", "context_only"]
    assert policy["knowledge_origin"] == "expert_practice"
    assert _verify_prepared_input_manifest(input_root, output) == {
        "source_ledger_sha256": ledger_hash,
        "semantic_audit_sha256": manifest["semantic_audit_sha256"],
        "preparation_manifest_sha256": manifest["manifest_sha256"],
    }


def test_runner_rejects_modified_prepared_input(tmp_path: Path) -> None:
    staging, ledger_hash, audit, output = _staged_fixture(tmp_path)
    result = prepare_bootstrap_input(staging, ledger_hash, audit, output)
    input_root = Path(result["input_root"])
    candidate_path = input_root / "candidates" / "practitioner-candidates.json"
    candidate_path.write_text("[]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="file (hash|length) mismatch"):
        _verify_prepared_input_manifest(input_root, output)


def test_prepare_rejects_audit_that_allows_empirical_role(tmp_path: Path) -> None:
    staging, ledger_hash, audit, output = _staged_fixture(tmp_path)
    value = json.loads(audit.read_text())
    value["records"][0]["claim_roles_allowed"] = ["result", "context_only"]
    audit.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(BootstrapInputError) as raised:
        prepare_bootstrap_input(staging, ledger_hash, audit, output)

    assert raised.value.code == "unsafe_audit_claim_role"
    assert not output.exists()


def test_v02_audit_requires_and_binds_exact_content_hash(tmp_path: Path) -> None:
    staging, ledger_hash, audit, output = _staged_fixture(tmp_path)
    ledger = json.loads((staging / "ledgers" / f"{ledger_hash}.json").read_text())
    content_hash = ledger["entries"][0]["exact_content"]["content_sha256"]
    value = json.loads(audit.read_text())
    value["schema_version"] = "magicforge-practitioner-exact-content-audit-0.2"
    value["scope"] = {
        "discovered_candidates_reviewed": 2,
        "eligible_exact_content": 1,
        "blocked_discovery_only": 1,
        "database_write": False,
        "glm_called": False,
        "approval_created": False,
        "qdrant_write": False,
    }
    value["policy"] = {
        "knowledge_origin": "expert_practice",
        "peer_review_status": "not_applicable",
        "source_eligibility_is_not_claim_approval": True,
        "practitioner_source_is_never_empirical_evidence": True,
        "unknown_authors_are_not_inferred": True,
        "public_availability_does_not_grant_redistribution": True,
        "operational_or_restricted_secret_detail": (
            "blocked_from_exact_content_staging"
        ),
    }
    eligible = value["records"][0]
    eligible.pop("verification")
    eligible.update(
        {
            "knowledge_origin": "expert_practice",
            "empirical_evidence_allowed": False,
            "sensitivity": "controlled",
            "content_sha256": content_hash,
        }
    )
    value["records"].append(
        {
            "url": "https://magician.example.org/blocked",
            "title": "Blocked source",
            "disposition": "blocked_discovery_only",
            "blocked_reason": "restricted_secret_detail",
            "exact_content_staged": False,
            "source_payload_allowed": False,
        }
    )
    audit.write_text(json.dumps(value), encoding="utf-8")

    prepared = prepare_bootstrap_input(staging, ledger_hash, audit, output)
    assert prepared["candidates_prepared"] == 1

    bad_output = tmp_path / "other" / "bootstrap-003"
    value["records"][0]["content_sha256"] = "0" * 64
    audit.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(BootstrapInputError) as raised:
        prepare_bootstrap_input(staging, ledger_hash, audit, bad_output)
    assert raised.value.code == "audit_content_hash_mismatch"


def test_explicit_execution_keeps_qdrant_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staging, ledger_hash, audit, output = _staged_fixture(tmp_path)
    prepared = prepare_bootstrap_input(staging, ledger_hash, audit, output)
    observed = {}
    manifest = json.loads(Path(prepared["manifest_file"]).read_text())
    lineage = {
        "source_ledger_sha256": manifest["source_ledger_sha256"],
        "semantic_audit_sha256": manifest["semantic_audit_sha256"],
        "preparation_manifest_sha256": manifest["manifest_sha256"],
    }

    def fake_run(**kwargs):
        observed.update(kwargs)
        return {
            "run_id": "bootstrap-003",
            "input_lineage": [lineage],
            "qdrant_ingestion_attempted": False,
            "safety": {"production_collection_touched": False},
        }

    monkeypatch.setattr("research.bootstrap.runner.run", fake_run)
    report = execute_prepared_bootstrap(prepared, output_root=output)

    assert report["run_id"] == "bootstrap-003"
    assert observed["ingest_qdrant"] is False
    assert observed["source_run"] == Path(prepared["input_root"])


def test_preparation_supports_a_new_isolated_expansion_run(tmp_path: Path) -> None:
    staging, ledger_hash, audit, _ = _staged_fixture(tmp_path)
    output = tmp_path / "bootstrap-004"

    prepared = prepare_bootstrap_input(staging, ledger_hash, audit, output)
    manifest = json.loads(Path(prepared["manifest_file"]).read_text())

    assert prepared["run_id"] == "bootstrap-004"
    assert manifest["run_id"] == "bootstrap-004"
    assert Path(prepared["input_root"]).is_relative_to(output)


@pytest.mark.parametrize(
    "name",
    ["bootstrap-001", "bootstrap-002", "bootstrap-0004", "run-004"],
)
def test_preparation_rejects_foundation_or_malformed_run_identity(
    tmp_path: Path,
    name: str,
) -> None:
    staging, ledger_hash, audit, _ = _staged_fixture(tmp_path)

    with pytest.raises(BootstrapInputError) as raised:
        prepare_bootstrap_input(
            staging,
            ledger_hash,
            audit,
            tmp_path / name,
        )

    assert raised.value.code in {
        "authoritative_run_is_immutable",
        "invalid_output_identity",
    }


@pytest.mark.parametrize("foundation", ["bootstrap-001", "bootstrap-002"])
def test_preparation_rejects_output_nested_below_foundation_run(
    tmp_path: Path,
    foundation: str,
) -> None:
    staging, ledger_hash, audit, _ = _staged_fixture(tmp_path)
    output = tmp_path / foundation / "bootstrap-004"

    with pytest.raises(BootstrapInputError) as raised:
        prepare_bootstrap_input(staging, ledger_hash, audit, output)

    assert raised.value.code == "authoritative_run_is_immutable"
    assert not output.exists()


def test_preparation_rejects_nested_output_symlink_escape(
    tmp_path: Path,
) -> None:
    staging, ledger_hash, audit, _ = _staged_fixture(tmp_path)
    output = tmp_path / "bootstrap-004"
    input_root = output / "input_batches" / ledger_hash
    input_root.mkdir(parents=True)
    escaped = tmp_path / "escaped-foundation"
    escaped.mkdir()
    (input_root / "candidates").symlink_to(escaped, target_is_directory=True)

    with pytest.raises(BootstrapInputError) as raised:
        prepare_bootstrap_input(staging, ledger_hash, audit, output)

    assert raised.value.code == "output_path_not_isolated"
    assert list(escaped.iterdir()) == []


def test_execution_rejects_cross_run_preparation_before_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging, ledger_hash, audit, output = _staged_fixture(tmp_path)
    prepared = prepare_bootstrap_input(staging, ledger_hash, audit, output)
    runner_called = False

    def fake_run(**_kwargs):
        nonlocal runner_called
        runner_called = True
        raise AssertionError("runner must not execute")

    monkeypatch.setattr("research.bootstrap.runner.run", fake_run)
    with pytest.raises(BootstrapInputError) as raised:
        execute_prepared_bootstrap(
            prepared,
            output_root=tmp_path / "bootstrap-004",
        )

    assert raised.value.code == "preparation_run_identity_mismatch"
    assert runner_called is False


@pytest.mark.parametrize(
    ("descriptor_field", "error_code"),
    [
        ("manifest_file", "preparation_manifest_binding_mismatch"),
        ("content_bundle", "preparation_content_binding_mismatch"),
    ],
)
def test_execution_rejects_unbound_descriptor_paths_before_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor_field: str,
    error_code: str,
) -> None:
    staging, ledger_hash, audit, output = _staged_fixture(tmp_path)
    prepared = prepare_bootstrap_input(staging, ledger_hash, audit, output)
    tampered = {**prepared, descriptor_field: str(tmp_path / "unbound.json")}
    runner_called = False

    def fake_run(**_kwargs):
        nonlocal runner_called
        runner_called = True
        raise AssertionError("runner must not execute")

    monkeypatch.setattr("research.bootstrap.runner.run", fake_run)
    with pytest.raises(BootstrapInputError) as raised:
        execute_prepared_bootstrap(tampered, output_root=output)

    assert raised.value.code == error_code
    assert runner_called is False


@pytest.mark.parametrize(
    ("report_change", "error_code"),
    [
        ({"run_id": "bootstrap-003"}, "execution_run_identity_mismatch"),
        ({"qdrant_ingestion_attempted": True}, "qdrant_boundary_violation"),
        (
            {"safety": {"production_collection_touched": True}},
            "production_boundary_violation",
        ),
    ],
)
def test_expansion_execution_rejects_reported_boundary_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_change: dict[str, object],
    error_code: str,
) -> None:
    staging, ledger_hash, audit, _ = _staged_fixture(tmp_path)
    output = tmp_path / "bootstrap-004"
    prepared = prepare_bootstrap_input(staging, ledger_hash, audit, output)
    manifest = json.loads(Path(prepared["manifest_file"]).read_text())
    lineage = {
        "source_ledger_sha256": manifest["source_ledger_sha256"],
        "semantic_audit_sha256": manifest["semantic_audit_sha256"],
        "preparation_manifest_sha256": manifest["manifest_sha256"],
    }
    observed = {}

    def fake_run(**kwargs):
        observed.update(kwargs)
        report = {
            "run_id": "bootstrap-004",
            "input_lineage": [lineage],
            "qdrant_ingestion_attempted": False,
            "safety": {"production_collection_touched": False},
        }
        report.update(report_change)
        return report

    monkeypatch.setattr("research.bootstrap.runner.run", fake_run)
    with pytest.raises(BootstrapInputError) as raised:
        execute_prepared_bootstrap(prepared, output_root=output)

    assert raised.value.code == error_code
    assert observed["ingest_qdrant"] is False


def test_prepare_academic_input_binds_audits_and_scientific_claim_roles(
    tmp_path: Path,
) -> None:
    staging, ledger_hash, audit, independent, output = _academic_staged_fixture(
        tmp_path
    )

    prepared = prepare_academic_bootstrap_input(
        staging, ledger_hash, audit, independent, output
    )

    assert prepared["candidates_prepared"] == 1
    input_root = Path(prepared["input_root"])
    candidates = json.loads(
        (input_root / "candidates" / "academic-candidates.json").read_text()
    )
    practitioner_candidates = json.loads(
        (input_root / "candidates" / "practitioner-candidates.json").read_text()
    )
    sources = json.loads(
        (input_root / "sources" / "verified-academic-sources.json").read_text()
    )
    policies = _load_source_claim_policies([input_root])
    policy = policies[candidates[0]["id"]]
    manifest = json.loads(Path(prepared["manifest_file"]).read_text())

    assert practitioner_candidates == []
    assert sources[0]["source_category"] == "academic"
    assert sources[0]["knowledge_origin"] == "scientific_evidence"
    assert sources[0]["citation"]["peer_review_status"] == "peer_reviewed"
    assert policy["knowledge_origin"] == "scientific_evidence"
    assert policy["allowed_claim_roles"] == [
        "result",
        "method",
        "background",
        "hypothesis",
        "discussion",
        "context_only",
    ]
    assert "expert_opinion" not in policy["allowed_claim_roles"]
    assert policy["published_year"] == 2017
    assert policy["doi"] == "10.1234/example.corrected"
    assert manifest["independent_audit_sha256"]
    assert _verify_prepared_input_manifest(input_root, output) == {
        "source_ledger_sha256": ledger_hash,
        "semantic_audit_sha256": manifest["semantic_audit_sha256"],
        "preparation_manifest_sha256": manifest["manifest_sha256"],
        "independent_audit_sha256": manifest["independent_audit_sha256"],
    }


def test_prepare_academic_input_allows_no_metadata_corrections(
    tmp_path: Path,
) -> None:
    staging, ledger_hash, audit, independent, output = _academic_staged_fixture(
        tmp_path
    )
    full_value = json.loads(audit.read_text())
    full_value["bibliographic_corrections"] = []
    audit.write_text(json.dumps(full_value), encoding="utf-8")
    independent_value = json.loads(independent.read_text())
    independent_value.pop("resolved_metadata_issue")
    independent_value["ledger_history"].pop("superseded")
    independent_value["records"][0]["metadata"] = "pass"
    independent.write_text(json.dumps(independent_value), encoding="utf-8")

    prepared = prepare_academic_bootstrap_input(
        staging,
        ledger_hash,
        audit,
        independent,
        output,
    )

    assert prepared["candidates_prepared"] == 1


def test_prepare_academic_input_accepts_generic_year_reconciliation_status(
    tmp_path: Path,
) -> None:
    staging, ledger_hash, audit, independent, output = _academic_staged_fixture(
        tmp_path
    )
    independent_value = json.loads(independent.read_text())
    independent_value["records"][0]["metadata"] = "pass_year_reconciled"
    independent.write_text(json.dumps(independent_value), encoding="utf-8")

    prepared = prepare_academic_bootstrap_input(
        staging,
        ledger_hash,
        audit,
        independent,
        output,
    )

    assert prepared["candidates_prepared"] == 1


def test_prepare_academic_input_rejects_content_length_audit_drift(
    tmp_path: Path,
) -> None:
    staging, ledger_hash, audit, independent, output = _academic_staged_fixture(
        tmp_path
    )
    value = json.loads(independent.read_text())
    value["records"][0]["content_bytes"] += 1
    independent.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(BootstrapInputError) as raised:
        prepare_academic_bootstrap_input(
            staging, ledger_hash, audit, independent, output
        )

    assert raised.value.code == "academic_content_length_mismatch"


def test_combined_execution_passes_both_batches_and_keeps_qdrant_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    practitioner_staging, practitioner_ledger, practitioner_audit, output = (
        _staged_fixture(tmp_path)
    )
    academic_staging, academic_ledger, academic_audit, independent, _ = (
        _academic_staged_fixture(tmp_path)
    )
    practitioner = prepare_bootstrap_input(
        practitioner_staging, practitioner_ledger, practitioner_audit, output
    )
    academic = prepare_academic_bootstrap_input(
        academic_staging, academic_ledger, academic_audit, independent, output
    )
    observed = {}
    lineages = []
    for prepared in (practitioner, academic):
        manifest = json.loads(Path(prepared["manifest_file"]).read_text())
        lineage = {
            "source_ledger_sha256": manifest["source_ledger_sha256"],
            "semantic_audit_sha256": manifest["semantic_audit_sha256"],
            "preparation_manifest_sha256": manifest["manifest_sha256"],
        }
        if manifest["independent_audit_sha256"] is not None:
            lineage["independent_audit_sha256"] = manifest[
                "independent_audit_sha256"
            ]
        lineages.append(lineage)

    def fake_run(**kwargs):
        observed.update(kwargs)
        return {
            "run_id": "bootstrap-003",
            "input_lineage": lineages,
            "qdrant_ingestion_attempted": False,
            "safety": {"production_collection_touched": False},
        }

    monkeypatch.setattr("research.bootstrap.runner.run", fake_run)
    report = execute_prepared_bootstrap_batches(
        [practitioner, academic], output_root=output
    )

    assert report["run_id"] == "bootstrap-003"
    assert observed["ingest_qdrant"] is False
    assert observed["source_run"] == Path(practitioner["input_root"])
    assert observed["additional_source_runs"] == [Path(academic["input_root"])]
    assert observed["additional_content_bundles"] == [
        Path(academic["content_bundle"])
    ]


def test_runner_rejects_superseded_practitioner_manifest_directly(
    tmp_path: Path,
) -> None:
    staging, ledger_hash, audit, output = _staged_fixture(tmp_path)
    prepared = prepare_bootstrap_input(staging, ledger_hash, audit, output)
    manifest_path = Path(prepared["manifest_file"])
    manifest = json.loads(manifest_path.read_text())
    manifest["source_ledger_sha256"] = (
        "e4830a43cf8853ab5aadde246335a3ba5ec07284735531d5ac7398361cb7c590"
    )
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="superseded prepared bootstrap input"):
        _verify_prepared_input_manifest(Path(prepared["input_root"]), output)


def test_runner_blocks_bootstrap006_mystery_card_preparation() -> None:
    assert (
        "4f93818e16ec090f31a08cc5b2ed9dd91ae45991801cbae52d8d38247cd0357a"
        in SUPERSEDED_PREPARED_INPUT_LEDGERS
    )


@pytest.mark.parametrize(
    ("origin", "roles"),
    [
        ("scientific_evidence", ["expert_opinion", "context_only"]),
        ("expert_practice", ["result", "context_only"]),
    ],
)
def test_runner_rejects_claim_roles_outside_origin_boundary(
    tmp_path: Path, origin: str, roles: list[str]
) -> None:
    source_run = tmp_path / "input"
    _write(
        source_run / "policies" / "source-claim-policies.json",
        {
            "schema_version": "bootstrap-source-claim-policy-0.1",
            "sources": {
                "candidate-1": {
                    "candidate_id": "candidate-1",
                    "knowledge_origin": origin,
                    "allowed_claim_roles": roles,
                }
            },
        },
    )

    with pytest.raises(ValueError, match="knowledge-origin boundary"):
        _load_source_claim_policies([source_run])


class _PolicyGLM:
    model = "glm-policy-test"

    def generate(self, *_args, **_kwargs) -> str:
        excerpt = (
            "The performer recommends directing the audience's attention before a move."
        )
        return ResearchExtractionResult(
            claims=[
                ExtractedClaim(
                    statement=excerpt,
                    evidence_excerpt=excerpt,
                    confidence=0.9,
                    claim_role=ClaimRole.EXPERT_OPINION,
                    evidence_class=EvidenceClass.EXPERT_INSTRUCTION,
                    applicable_domains=[MagicDomain.THEORY],
                    ontology_paths=["Performance.AttentionDirection"],
                    limitations=["Single practitioner's account."],
                ),
                ExtractedClaim(
                    statement="This account reports that attention direction preceded a move.",
                    evidence_excerpt="This account reports that attention direction preceded a move.",
                    confidence=0.8,
                    claim_role=ClaimRole.RESULT,
                    evidence_class=EvidenceClass.PRACTITIONER_REPORT,
                    applicable_domains=[MagicDomain.THEORY],
                    ontology_paths=["Performance.AttentionDirection"],
                    limitations=["Not independently verified."],
                ),
            ]
        ).model_dump_json()


def test_strict_source_policy_drops_disallowed_claim_roles() -> None:
    content = (
        "The performer recommends directing the audience's attention before a move. "
        "This account reports that attention direction preceded a move."
    )
    candidate = ResearchCandidate(
        title="Notes on directing attention",
        url="https://magician.example.org/attention",
        authors=["Expert Magician"],
        content=content,
        content_access=ContentAccess.WEB_EXTRACT,
        source_category=SourceCategory.PRACTITIONER,
        provenance=[
            SearchProvenance(
                provider=DiscoveryProvider.TAVILY,
                tool_name="tavily_extract",
                query="magician attention essay",
            )
        ],
    )
    citation = CitationManager().from_candidate(candidate)
    approval = BootstrapSourceRegistrar(MagicForgeMode.BOOTSTRAP).register(
        candidate, citation
    )
    proposal = ResearchKnowledgeExtractor(
        _PolicyGLM(), mode=MagicForgeMode.BOOTSTRAP, chunk_size=2_000
    ).extract_candidate(candidate, citation, approval)

    filtered, rejections = _apply_source_claim_policy(
        proposal,
        {
            "allowed_claim_roles": ["expert_opinion", "context_only"],
            "knowledge_origin": KnowledgeOrigin.EXPERT_PRACTICE.value,
        },
    )

    assert [card.claim_role for card in filtered.evidence_cards] == [
        ClaimRole.EXPERT_OPINION
    ]
    assert all(
        card.knowledge_origin == KnowledgeOrigin.EXPERT_PRACTICE
        for card in filtered.evidence_cards
    )
    assert len(rejections) == 1
    assert "claim_role result" in rejections[0]["reason"]


def test_runner_passes_configured_glm_transport_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = ResearchCandidate(
        title="Notes on directing attention",
        url="https://magician.example.org/transport",
        source_category=SourceCategory.PRACTITIONER,
        provenance=[
            SearchProvenance(
                provider=DiscoveryProvider.TAVILY,
                tool_name="tavily_extract",
                query="magic performance",
            )
        ],
    )
    citation = CitationManager().from_candidate(candidate)
    for name in ("sources", "extracted_claims", "evidence_cards", "knowledge_nodes"):
        (tmp_path / name).mkdir()
    observed = {}

    def stop_after_client(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        raise RuntimeError("client captured")

    monkeypatch.setattr("research.bootstrap.runner.GLMClient", stop_after_client)
    with pytest.raises(RuntimeError, match="client captured"):
        _process_candidate(
            sequence=1,
            candidate=candidate,
            citation=citation,
            acquired="A complete practitioner account about attention direction.",
            output=tmp_path,
            mode=MagicForgeMode.BOOTSTRAP,
            glm_api_key="test-key",
            glm_model="glm-test",
            glm_timeout_seconds=247.5,
            glm_max_retries=4,
            max_source_characters=8_000,
            force_reprocess=False,
            reprocess_candidate=False,
            request_interval_seconds=0.0,
        )

    assert observed == {
        "args": ("test-key", "glm-test"),
        "kwargs": {"timeout_seconds": 247.5, "max_retries": 4},
    }
