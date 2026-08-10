import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from localization.pipeline.artifacts import ArtifactError, read_jsonl, write_json, write_jsonl
from localization.pipeline.cli import main
from localization.pipeline.inventory import collect_frontend_inventory
from localization.pipeline.models import (
    GenerationOrigin,
    ProposalStatus,
    QualityReviewDisposition,
    QualityReviewNormalization,
    TranslationProposal,
)
from localization.pipeline.policy import PROJECT_ROOT, load_policy_index
from localization.pipeline.reviewer import (
    GLMLocalizationQualityReviewer,
    load_verified_assembly_run,
    proposal_sha256,
    read_review_keys,
    select_review_candidates,
)
from localization.pipeline.validator import LocalizationValidator
from localization.pipeline.workflow import build_source_units


@pytest.fixture(scope="module")
def review_workspace():
    inventory = collect_frontend_inventory(PROJECT_ROOT)
    policy = load_policy_index()
    units = build_source_units(inventory, policy)
    payload = json.dumps(
        inventory["catalogs"]["enUS"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return inventory, policy, units, hashlib.sha256(payload.encode()).hexdigest()


def _proposal(unit, candidate: str) -> TranslationProposal:
    return TranslationProposal(
        key=unit.key,
        source_text=unit.source_text,
        source_hash=unit.source_hash,
        candidate_chinese=candidate,
        rationale="First-pass GLM candidate.",
        confidence=0.91,
        status=ProposalStatus.MACHINE_PROPOSED,
        provider="glm",
        protected_terms=unit.protected_terms,
        generated_by=GenerationOrigin.LOCAL_AI,
    )


def _write_assembly_run(
    root: Path,
    run_id: str,
    proposals: list[TranslationProposal],
    catalog_sha256: str,
) -> Path:
    run = root / run_id
    run.mkdir()
    proposal_path = run / "proposals.jsonl"
    write_jsonl(proposal_path, proposals)
    proposal_payload = proposal_path.read_bytes()
    write_json(
        run / "manifest.json",
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "command": "assemble",
            "status": "completed",
            "canonical_catalog_sha256": catalog_sha256,
            "unit_count": len(proposals),
            "proposal_count": len(proposals),
            "validation_passed": True,
            "llm_invoked": False,
            "upstream_llm_invoked": True,
            "proposal_provenance": "assembled_verified_glm_propose_runs",
            "external_translation_platform_used": False,
            "human_translator_used": False,
            "runtime_modified": False,
            "translation_memory_modified": False,
            "upstream_runs": [
                {
                    "run_id": "source-glm-run",
                    "llm_provider": "GLM",
                    "llm_model": "glm-test",
                    "manifest_sha256": "a" * 64,
                    "proposals_sha256": "b" * 64,
                }
            ],
            "artifacts": {
                "proposals.jsonl": {
                    "sha256": hashlib.sha256(proposal_payload).hexdigest(),
                    "bytes": len(proposal_payload),
                }
            },
        },
    )
    return run


def _write_quality_review_run(
    root: Path,
    run_id: str,
    units,
    catalog_sha256: str,
    *,
    selected_key: str,
    selected_candidate: str,
    final_candidate: str,
) -> Path:
    run = root / run_id
    run.mkdir()
    unit_by_key = {unit.key: unit for unit in units}
    validator = LocalizationValidator(load_policy_index())
    selected_proposal = _proposal(unit_by_key[selected_key], selected_candidate)
    final_proposals = [
        _proposal(
            unit,
            final_candidate
            if unit.key == selected_key
            else (
                unit.source_text
                if unit.action.value
                in {"preserve_exact", "preserve_english_only", "skip"}
                or unit.classification.value
                in {"source_citation", "knowledge_content", "technical_identifier"}
                else validator.canonicalize_source_terms(unit.source_text)
            ),
        )
        for unit in units
    ]
    selected_path = run / "proposals.jsonl"
    final_path = run / "final-proposals.jsonl"
    validation_path = run / "final-validation-report.json"
    decisions_path = run / "review-decisions.jsonl"
    write_jsonl(selected_path, [selected_proposal])
    write_jsonl(
        decisions_path,
        [
            {
                "key": selected_key,
                "model_disposition": "keep",
                "disposition": "keep",
                "normalization": None,
                "issue_types": [],
                "rationale": "Candidate retained by test GLM review.",
                "confidence": 0.9,
                "input_candidate_chinese": selected_candidate,
                "result_candidate_chinese": selected_candidate,
                "input_proposal_sha256": proposal_sha256(selected_proposal),
                "revised_proposal": None,
                "reviewer_provider": "glm",
            }
        ],
    )
    write_jsonl(final_path, final_proposals)
    write_json(
        validation_path,
        {
            "valid": True,
            "checked_units": len(final_proposals),
            "checked_proposals": len(final_proposals),
            "error_count": 0,
            "warning_count": 0,
            "issues": [],
        },
    )

    def fingerprint(path: Path) -> dict:
        payload = path.read_bytes()
        return {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}

    upstream = [
        {
            "run_id": "source-glm-run",
            "llm_provider": "GLM",
            "llm_model": "glm-test",
            "manifest_sha256": "a" * 64,
            "proposals_sha256": "b" * 64,
        }
    ]
    write_json(
        run / "manifest.json",
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "command": "quality-review",
            "status": "completed",
            "canonical_catalog_sha256": catalog_sha256,
            "unit_count": 1,
            "proposal_count": 1,
            "quality_review_count": 1,
            "final_proposal_count": len(final_proposals),
            "full_catalog_overlay": True,
            "validation_passed": True,
            "final_validation_passed": True,
            "llm_invoked": True,
            "llm_provider": "GLM",
            "llm_model": "glm-test",
            "proposal_provenance": "quality_review_over_verified_candidate_run",
            "external_translation_platform_used": False,
            "human_translator_used": False,
            "runtime_modified": False,
            "translation_memory_modified": False,
            "input_candidate_run": {
                "run_id": "prior-candidate-run",
                "path": str(root / "prior-candidate-run"),
                "command": "assemble",
                "proposals_artifact": "proposals.jsonl",
                "manifest_sha256": "c" * 64,
                "proposals_sha256": "d" * 64,
                "proposal_count": len(final_proposals),
                "canonical_catalog_sha256": catalog_sha256,
                "upstream_runs": upstream,
            },
            "artifacts": {
                "proposals.jsonl": fingerprint(selected_path),
                "review-decisions.jsonl": fingerprint(decisions_path),
                "final-proposals.jsonl": fingerprint(final_path),
                "final-validation-report.json": fingerprint(validation_path),
            },
        },
    )
    return run


class ReviewFakeGLM:
    def __init__(self, decisions_by_key):
        self.decisions_by_key = decisions_by_key
        self.calls = []

    def generate_structured(self, prompt, response_model, **kwargs):
        self.calls.append((prompt, kwargs))
        payload = json.loads(prompt.split("REVIEW_INPUT:\n", 1)[1])
        decisions = [self.decisions_by_key[unit["key"]] for unit in payload["units"]]
        return response_model.model_validate({"decisions": decisions})


def test_quality_reviewer_structures_keep_revise_and_human_review(review_workspace) -> None:
    _, policy, units, _ = review_workspace
    by_key = {unit.key: unit for unit in units}
    selected = [
        by_key["chat.composer.submit"],
        by_key["shell.mobileDescription"],
        by_key["evidence.header.collection"],
    ]
    proposals = [
        _proposal(selected[0], "放下问题牌"),
        _proposal(selected[1], "魔术认知与性能知识仪器"),
        _proposal(selected[2], "收集"),
    ]
    fake = ReviewFakeGLM(
        {
            "chat.composer.submit": {
                "key": "chat.composer.submit",
                "disposition": "keep",
                "issue_types": [],
                "rationale": "语义与工作台动作一致。",
                "confidence": 0.94,
                "revised_candidate_chinese": None,
            },
            "shell.mobileDescription": {
                "key": "shell.mobileDescription",
                "disposition": "revise",
                "issue_types": ["domain_sense", "semantic_mistranslation"],
                "rationale": "Performance 在此指魔术表演，而非计算性能。",
                "confidence": 0.99,
                "revised_candidate_chinese": "魔术认知与表演知识仪器",
            },
            "evidence.header.collection": {
                "key": "evidence.header.collection",
                "disposition": "needs_human_review",
                "issue_types": ["ambiguity"],
                "rationale": "需确认这里指馆藏还是存储 Collection。",
                "confidence": 0.77,
                "revised_candidate_chinese": None,
            },
        }
    )

    result = GLMLocalizationQualityReviewer(
        fake,
        validator=LocalizationValidator(policy),
    ).review(selected, proposals)

    assert [item.disposition for item in result.decisions] == [
        QualityReviewDisposition.KEEP,
        QualityReviewDisposition.REVISE,
        QualityReviewDisposition.NEEDS_HUMAN_REVIEW,
    ]
    assert result.proposals[0] == proposals[0]
    assert result.proposals[1].candidate_chinese == "魔术认知与表演知识仪器"
    assert result.proposals[1].status == ProposalStatus.MACHINE_PROPOSED
    assert result.proposals[1].provider == "glm"
    assert result.proposals[2] == proposals[2]
    assert result.decisions[1].revised_proposal == result.proposals[1]
    assert fake.calls[0][1]["temperature"] == 0.0
    sent = json.loads(fake.calls[0][0].split("REVIEW_INPUT:\n", 1)[1])
    assert set(sent["units"][0]) >= {
        "current_runtime_target",
        "machine_candidate",
        "context",
    }
    assert "confidence" not in sent["units"][0]


def test_long_raw_rationale_is_bounded_for_decision_and_revision(
    review_workspace,
) -> None:
    _, policy, units, _ = review_workspace
    unit = next(item for item in units if item.key == "shell.mobileDescription")
    original = _proposal(unit, "魔术认知与性能知识仪器")
    raw_rationale = ("Performance 在这里表示魔术表演，而不是计算性能。 " * 45).strip()
    assert 500 < len(raw_rationale) < 2_000
    fake = ReviewFakeGLM(
        {
            unit.key: {
                "key": unit.key,
                "disposition": "revise",
                "issue_types": ["domain_sense"],
                "rationale": raw_rationale,
                "confidence": 0.97,
                "revised_candidate_chinese": "魔术认知与表演知识仪器",
            }
        }
    )

    result = GLMLocalizationQualityReviewer(
        fake,
        validator=LocalizationValidator(policy),
    ).review([unit], [original])

    decision = result.decisions[0]
    assert decision.disposition == QualityReviewDisposition.REVISE
    assert len(decision.rationale) <= 500
    assert decision.rationale.endswith("[truncated]")
    assert decision.revised_proposal is not None
    assert decision.revised_proposal.rationale == decision.rationale
    assert result.proposals[0].rationale == decision.rationale


def test_invalid_protected_term_revision_is_normalized_to_human_review(
    review_workspace,
) -> None:
    _, policy, units, _ = review_workspace
    unit = next(item for item in units if item.key == "chat.header.effect")
    original = _proposal(unit, "Effect")
    fake = ReviewFakeGLM(
        {
            unit.key: {
                "key": unit.key,
                "disposition": "revise",
                "issue_types": ["terminology"],
                "rationale": "Invalid test revision.",
                "confidence": 0.8,
                "revised_candidate_chinese": "效果",
            }
        }
    )

    result = GLMLocalizationQualityReviewer(
        fake,
        validator=LocalizationValidator(policy),
    ).review([unit], [original])

    decision = result.decisions[0]
    assert decision.model_disposition == QualityReviewDisposition.REVISE
    assert decision.disposition == QualityReviewDisposition.NEEDS_HUMAN_REVIEW
    assert decision.normalization == QualityReviewNormalization.INVALID_REVISION_POLICY
    assert decision.normalization_issue_codes
    assert "english_only_term_not_preserved" in decision.normalization_issue_codes
    assert "protected_term_risk" in [issue.value for issue in decision.issue_types]
    assert "terminology" in [issue.value for issue in decision.issue_types]
    assert "invalid revision policy" in decision.rationale
    assert decision.revised_proposal is None
    assert result.proposals == (original,)


def test_limited_bilingual_revision_failure_preserves_source_candidate(
    review_workspace,
) -> None:
    _, policy, units, _ = review_workspace
    unit = next(item for item in units if item.key == "chat.evidence.traceTitle")
    original = _proposal(unit, "追踪回答如何抵达这些 Source")
    fake = ReviewFakeGLM(
        {
            unit.key: {
                "key": unit.key,
                "disposition": "revise",
                "issue_types": ["unnatural_ui_copy"],
                "rationale": "调整为更自然的界面表达。",
                "confidence": 0.9,
                "revised_candidate_chinese": "追踪此答案的来源路径",
            }
        }
    )

    result = GLMLocalizationQualityReviewer(
        fake,
        validator=LocalizationValidator(policy),
    ).review([unit], [original])

    decision = result.decisions[0]
    assert decision.disposition == QualityReviewDisposition.NEEDS_HUMAN_REVIEW
    assert decision.model_disposition == QualityReviewDisposition.REVISE
    assert decision.normalization == QualityReviewNormalization.INVALID_REVISION_POLICY
    assert "limited_bilingual_english_not_preserved" in (
        decision.normalization_issue_codes
    )
    assert "unapproved_term_display" in decision.normalization_issue_codes
    assert [issue.value for issue in decision.issue_types] == [
        "unnatural_ui_copy",
        "protected_term_risk",
    ]
    assert "limited_bilingual_english_not_preserved" in decision.rationale
    assert decision.result_candidate_chinese == original.candidate_chinese
    assert decision.revised_proposal is None
    assert result.proposals == (original,)


def test_unchanged_revision_is_normalized_to_human_review(review_workspace) -> None:
    _, policy, units, _ = review_workspace
    unit = next(item for item in units if item.key == "dashboard.hero.fragments")
    original = _proposal(unit, "经审计的投影片段")
    issue_types = ["context_mismatch", "unnatural_ui_copy"]
    fake = ReviewFakeGLM(
        {
            unit.key: {
                "key": unit.key,
                "disposition": "revise",
                "issue_types": issue_types,
                "rationale": "候选需要调整以符合仪表语境。",
                "confidence": 0.82,
                "revised_candidate_chinese": original.candidate_chinese,
            }
        }
    )

    result = GLMLocalizationQualityReviewer(
        fake,
        validator=LocalizationValidator(policy),
    ).review([unit], [original])

    decision = result.decisions[0]
    assert decision.model_disposition == QualityReviewDisposition.REVISE
    assert decision.disposition == QualityReviewDisposition.NEEDS_HUMAN_REVIEW
    assert (
        decision.normalization
        == QualityReviewNormalization.UNCHANGED_REVISION_CONFLICT
    )
    assert [issue.value for issue in decision.issue_types] == issue_types
    assert "unchanged revision conflict" in decision.rationale
    assert decision.revised_proposal is None
    assert decision.result_candidate_chinese == original.candidate_chinese
    assert result.proposals == (original,)


def test_missing_revision_candidate_is_normalized_to_human_review(
    review_workspace,
) -> None:
    _, policy, units, _ = review_workspace
    unit = next(item for item in units if item.key == "research.runs.metricBasis")
    original = _proposal(unit, "指标基础")
    issue_types = ["context_mismatch"]
    raw_rationale = ("候选需要按运行指标语境修订。 " * 80).strip()
    fake = ReviewFakeGLM(
        {
            unit.key: {
                "key": unit.key,
                "disposition": "revise",
                "issue_types": issue_types,
                "rationale": raw_rationale,
                "confidence": 0.81,
                # Deliberately omit revised_candidate_chinese to reproduce the
                # incomplete structured response seen in remediation.
            }
        }
    )

    result = GLMLocalizationQualityReviewer(
        fake,
        validator=LocalizationValidator(policy),
    ).review([unit], [original])

    decision = result.decisions[0]
    assert decision.model_disposition == QualityReviewDisposition.REVISE
    assert decision.disposition == QualityReviewDisposition.NEEDS_HUMAN_REVIEW
    assert (
        decision.normalization
        == QualityReviewNormalization.MISSING_REVISION_CANDIDATE
    )
    assert [issue.value for issue in decision.issue_types] == issue_types
    assert "missing revision candidate" in decision.rationale
    assert decision.rationale.endswith("revised_candidate_chinese.")
    assert len(decision.rationale) <= 500
    assert decision.revised_proposal is None
    assert decision.result_candidate_chinese == original.candidate_chinese
    assert result.proposals == (original,)


def test_verified_assembly_hash_and_selection_gates(
    tmp_path: Path, review_workspace
) -> None:
    _, _, units, catalog_hash = review_workspace
    by_key = {unit.key: unit for unit in units}
    unchanged = _proposal(
        by_key["chat.composer.submit"],
        by_key["chat.composer.submit"].current_target or "提交",
    )
    changed = _proposal(by_key["shell.mobileDescription"], "待复核候选")
    run = _write_assembly_run(
        tmp_path,
        "verified-assembly",
        [unchanged, changed],
        catalog_hash,
    )
    verified = load_verified_assembly_run(
        run,
        units,
        canonical_catalog_sha256=catalog_hash,
    )

    selected_units, selected_proposals = select_review_candidates(
        verified,
        units,
        changed_only=True,
    )
    assert [unit.key for unit in selected_units] == ["shell.mobileDescription"]
    assert [proposal.key for proposal in selected_proposals] == [
        "shell.mobileDescription"
    ]

    with (run / "proposals.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("\n")
    with pytest.raises(ArtifactError, match="SHA-256 mismatch"):
        load_verified_assembly_run(
            run,
            units,
            canonical_catalog_sha256=catalog_hash,
        )


def test_completed_quality_review_is_safe_iterative_input(
    tmp_path: Path, review_workspace
) -> None:
    _, policy, units, catalog_hash = review_workspace
    key = "shell.mobileDescription"
    run = _write_quality_review_run(
        tmp_path,
        "completed-quality-review",
        units,
        catalog_hash,
        selected_key=key,
        selected_candidate="魔术认知与表演知识仪器",
        final_candidate="魔术认知与表演知识仪器",
    )

    verified = load_verified_assembly_run(
        run,
        units,
        canonical_catalog_sha256=catalog_hash,
    )
    selected_units, selected_proposals = select_review_candidates(
        verified,
        units,
        keys=[key],
    )

    assert verified.command == "quality-review"
    assert verified.proposals_artifact == "final-proposals.jsonl"
    assert len(verified.proposals) == len(units)
    assert selected_units[0].key == key
    assert (
        selected_proposals[0].candidate_chinese
        == "魔术认知与表演知识仪器"
    )
    assert LocalizationValidator(policy).validate_batch(
        list(units), list(verified.proposals)
    ).valid


def test_iterative_quality_review_rejects_tampered_final_artifact(
    tmp_path: Path, review_workspace
) -> None:
    _, _, units, catalog_hash = review_workspace
    run = _write_quality_review_run(
        tmp_path,
        "tampered-quality-review",
        units,
        catalog_hash,
        selected_key="shell.mobileDescription",
        selected_candidate="候选",
        final_candidate="魔术认知与表演知识仪器",
    )
    with (run / "final-proposals.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("\n")

    with pytest.raises(ArtifactError, match="final-proposals.jsonl SHA-256 mismatch"):
        load_verified_assembly_run(
            run,
            units,
            canonical_catalog_sha256=catalog_hash,
        )


def test_iterative_quality_review_requires_final_validation_pass(
    tmp_path: Path, review_workspace
) -> None:
    _, _, units, catalog_hash = review_workspace
    run = _write_quality_review_run(
        tmp_path,
        "failed-final-validation",
        units,
        catalog_hash,
        selected_key="shell.mobileDescription",
        selected_candidate="候选",
        final_candidate="魔术认知与表演知识仪器",
    )
    manifest_path = run / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["final_validation_passed"] = False
    write_json(manifest_path, manifest)

    with pytest.raises(ArtifactError, match="did not pass final validation"):
        load_verified_assembly_run(
            run,
            units,
            canonical_catalog_sha256=catalog_hash,
        )


def test_keys_file_is_strict_and_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "review-keys.txt"
    path.write_text(
        "# semantic audit\nshell.mobileDescription\n\nshell.mobileDescription\n",
        encoding="utf-8",
    )
    assert read_review_keys(path) == ("shell.mobileDescription",)
    path.write_text("shell.mobileDescription bad\n", encoding="utf-8")
    with pytest.raises(ArtifactError, match="invalid whitespace"):
        read_review_keys(path)


def test_quality_review_cli_without_use_glm_creates_plan_only(
    tmp_path: Path, review_workspace
) -> None:
    _, _, units, catalog_hash = review_workspace
    unit = next(item for item in units if item.key == "shell.mobileDescription")
    input_run = _write_assembly_run(
        tmp_path,
        "plan-input-assembly",
        [_proposal(unit, "魔术认知与性能知识仪器")],
        catalog_hash,
    )

    result = main(
        [
            "quality-review",
            "--input-run",
            str(input_run),
            "--key",
            unit.key,
            "--run-id",
            "quality-review-plan",
            "--output-root",
            str(tmp_path),
        ]
    )

    run = tmp_path / "quality-review-plan"
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert result == 0
    assert manifest["command"] == "quality-review"
    assert manifest["llm_invoked"] is False
    assert manifest["input_assembly"]["proposals_sha256"]
    assert manifest["review_selection"]["selected_keys"] == [unit.key]
    assert not (run / "proposals.jsonl").exists()
    assert not (run / "review-decisions.jsonl").exists()


def test_quality_review_cli_records_machine_only_decisions(
    tmp_path: Path, review_workspace, monkeypatch
) -> None:
    _, _, units, catalog_hash = review_workspace
    unit = next(item for item in units if item.key == "shell.mobileDescription")
    untouched_unit = next(item for item in units if item.key == "chat.composer.submit")
    untouched = _proposal(untouched_unit, "放下问题牌")
    input_run = _write_assembly_run(
        tmp_path,
        "glm-input-assembly",
        [_proposal(unit, "魔术认知与性能知识仪器"), untouched],
        catalog_hash,
    )
    fake = ReviewFakeGLM(
        {
            unit.key: {
                "key": unit.key,
                "disposition": "revise",
                "issue_types": ["domain_sense"],
                "rationale": "Performance 在此指魔术表演。",
                "confidence": 0.99,
                "revised_candidate_chinese": "魔术认知与表演知识仪器",
            }
        }
    )
    monkeypatch.setattr(
        "app.config.Settings.from_env",
        lambda: SimpleNamespace(
            glm_api_key="test-only",
            glm_model="glm-test",
            glm_timeout_seconds=5,
            glm_max_retries=0,
        ),
    )
    monkeypatch.setattr("llm.glm_client.GLMClient", lambda **_: fake)

    result = main(
        [
            "quality-review",
            "--input-run",
            str(input_run),
            "--key",
            unit.key,
            "--use-glm",
            "--run-id",
            "quality-review-result",
            "--output-root",
            str(tmp_path),
        ]
    )

    run = tmp_path / "quality-review-result"
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    decisions = read_jsonl(run / "review-decisions.jsonl")
    proposals = [
        TranslationProposal.model_validate(item)
        for item in read_jsonl(run / "proposals.jsonl")
    ]
    final_proposals = [
        TranslationProposal.model_validate(item)
        for item in read_jsonl(run / "final-proposals.jsonl")
    ]
    assert result == 0
    assert manifest["llm_invoked"] is True
    assert manifest["llm_provider"] == "GLM"
    assert manifest["review_counts"] == {
        "keep": 0,
        "revise": 1,
        "needs_human_review": 0,
    }
    assert manifest["review_normalization_count"] == 0
    assert manifest["review_normalization_counts"] == {}
    assert manifest["runtime_modified"] is False
    assert manifest["translation_memory_modified"] is False
    assert manifest["final_proposal_count"] == 2
    assert manifest["full_catalog_overlay"] is False
    assert manifest["final_validation_passed"] is True
    assert "review-decisions.jsonl" in manifest["artifacts"]
    assert "final-proposals.jsonl" in manifest["artifacts"]
    assert "final-validation-report.json" in manifest["artifacts"]
    assert decisions[0]["disposition"] == "revise"
    assert proposals[0].status == ProposalStatus.MACHINE_PROPOSED
    assert proposals[0].candidate_chinese == "魔术认知与表演知识仪器"
    assert [proposal.key for proposal in final_proposals] == [unit.key, untouched_unit.key]
    assert final_proposals[0] == proposals[0]
    assert final_proposals[1] == untouched
