import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from localization.pipeline.assembler import assemble_verified_runs
from localization.pipeline.cli import _safe_error_message, main
from localization.pipeline.artifacts import (
    ArtifactError,
    isolate_existing_run_artifacts,
    read_jsonl,
    write_json,
    write_jsonl,
)
from localization.pipeline.assets import load_localization_asset_manifest
from localization.pipeline.inventory import collect_frontend_inventory
from localization.pipeline.models import (
    GenerationOrigin,
    ProposalStatus,
    TranslationProposal,
)
from localization.pipeline.policy import PROJECT_ROOT, load_policy_index
from localization.pipeline.proposer import (
    GLMLocalizationProposer,
    LocalizationProposalError,
)
from localization.pipeline.validator import LocalizationValidator
from localization.pipeline.workflow import (
    audit_existing_targets,
    build_source_units,
)


@pytest.fixture(scope="module")
def inventory():
    return collect_frontend_inventory(PROJECT_ROOT)


@pytest.fixture(scope="module")
def governed_units(inventory):
    policy = load_policy_index()
    units = build_source_units(inventory, policy)
    return policy, units


def test_ast_inventory_has_catalog_and_placeholder_parity(inventory) -> None:
    assert inventory["ok"] is True
    assert inventory["summary"]["enUSKeys"] == inventory["summary"]["zhCNKeys"]
    assert inventory["summary"]["missingInZhCN"] == 0
    assert inventory["summary"]["extraInZhCN"] == 0
    assert inventory["summary"]["unknownUsedKeys"] == 0
    assert inventory["summary"]["placeholderMismatches"] == 0


def test_policy_loads_existing_assets_with_machine_approval_disabled() -> None:
    policy = load_policy_index()
    assert policy.schema_version == "1.2"
    assert policy.terms
    assert policy.machine_may_set_approved is False
    assert "MagicForge" in policy.exact_terms
    assert "Misdirection" in policy.english_only_terms


def test_all_existing_localization_assets_are_fingerprinted() -> None:
    manifest = load_localization_asset_manifest(PROJECT_ROOT)
    assert {
        "localization/glossary.en-zh.yaml",
        "localization/do-not-translate.yaml",
        "localization/translation-policy.md",
        "localization/brand-voice.md",
        "localization/translation-memory.jsonl",
        "localization/prompts/ui-localizer-v1.md",
        "localization/prompts/ui-quality-reviewer-v1.md",
    } <= set(manifest["files"])
    assert manifest["translation_memory"]["translation_unit_count"] == 0
    assert manifest["translation_memory"]["approved_translation_units"] == 0


def test_current_runtime_catalog_passes_canonical_term_governance(governed_units) -> None:
    policy, units = governed_units
    proposals, report = audit_existing_targets(units, LocalizationValidator(policy))
    assert len(units) == len(proposals)
    assert report.valid is True
    assert report.error_count == 0
    assert report.warning_count == 0
    assert report.issues == ()

    targets = {unit.key: unit.current_target for unit in units}
    assert targets["chat.composer.suggestionTwo"] == (
        "比较空间与时间 Misdirection 的 Cognitive Mechanism"
    )
    assert targets["research.stage.sources.shortReading"] == (
        "带 Provenance 的 Source"
    )


def test_variant_detection_is_category_scoped_and_returns_canonical_terms(
    governed_units,
) -> None:
    policy, _ = governed_units
    validator = LocalizationValidator(policy)

    assert validator.detect_protected_terms(
        "attention links cognitive mechanisms to claims and sources"
    ) == ("Attention", "Cognitive Mechanism", "Claim", "Source")
    assert validator.canonicalize_source_terms(
        "attention links cognitive mechanisms to claims and sources"
    ) == "Attention links Cognitive Mechanism to Claim and Source"
    assert validator.detect_protected_terms(
        "effects require methods and techniques"
    ) == ("Effect", "Method", "Technique")
    assert validator.detect_protected_terms(
        "load records, force refresh, switch views, and save"
    ) == ()
    assert validator.detect_protected_terms("Load, Force, Switch, and Save") == (
        "Load",
        "Force",
        "Switch",
    )
    assert validator.detect_protected_terms("glm opens magic chat") == ()
    assert validator.detect_protected_terms("GLM opens Magic Chat") == (
        "GLM",
        "Magic Chat",
    )


def test_source_variants_require_canonical_english_in_target(governed_units) -> None:
    policy, units = governed_units
    unit = next(item for item in units if item.key == "chat.turn.studyingDescription")
    validator = LocalizationValidator(policy)
    assert unit.protected_terms == ("Cognitive Mechanism",)

    valid = TranslationProposal(
        key=unit.key,
        source_text=unit.source_text,
        source_hash=unit.source_hash,
        candidate_chinese="正在追索记录、构造原则与 Cognitive Mechanism。",
        confidence=0.9,
        status=ProposalStatus.MACHINE_PROPOSED,
        provider="glm",
        protected_terms=unit.protected_terms,
        generated_by=GenerationOrigin.LOCAL_AI,
    )
    invalid = valid.model_copy(
        update={"candidate_chinese": "正在追索记录、构造原则与 cognitive mechanisms。"}
    )

    assert validator.validate_batch([unit], [valid]).valid is True
    report = validator.validate_batch([unit], [invalid])
    assert report.valid is False
    assert {issue.code for issue in report.issues} >= {
        "english_only_term_not_preserved"
    }


def test_placeholder_identifiers_do_not_count_as_terms(governed_units) -> None:
    policy, _ = governed_units
    validator = LocalizationValidator(policy)

    assert validator.detect_protected_terms("Locator: {locator}") == ("Locator",)
    assert validator.source_term_occurrence_count("Locator: {locator}", "Locator") == 1
    assert validator.detect_protected_terms("Value: {source}") == ()


def test_mixed_ui_unit_preserves_canonical_magic_term(governed_units) -> None:
    policy, units = governed_units
    unit = next(item for item in units if item.key == "chat.composer.suggestionTwo")
    validator = LocalizationValidator(policy)

    valid = TranslationProposal(
        key=unit.key,
        source_text=unit.source_text,
        source_hash=unit.source_hash,
        candidate_chinese="比较空间与时间 Misdirection 的 Cognitive Mechanism",
        confidence=0.9,
        status=ProposalStatus.MACHINE_PROPOSED,
        provider="glm",
        protected_terms=unit.protected_terms,
        generated_by=GenerationOrigin.LOCAL_AI,
    )
    invalid = valid.model_copy(update={"candidate_chinese": "比较空间与时间注意引导的认知机制"})

    assert validator.validate_batch([unit], [valid]).valid is True
    report = validator.validate_batch([unit], [invalid])
    assert report.valid is False
    assert {issue.code for issue in report.issues} >= {"english_only_term_not_preserved"}


def test_english_only_unit_rejects_appended_chinese_label(governed_units) -> None:
    policy, units = governed_units
    unit = next(item for item in units if item.key == "chat.header.effect")
    proposal = TranslationProposal(
        key=unit.key,
        source_text=unit.source_text,
        source_hash=unit.source_hash,
        candidate_chinese="Effect 中文标签",
        confidence=0.9,
        status=ProposalStatus.MACHINE_PROPOSED,
        provider="glm",
        protected_terms=unit.protected_terms,
        generated_by=GenerationOrigin.LOCAL_AI,
    )

    report = LocalizationValidator(policy).validate_batch([unit], [proposal])

    assert report.valid is False
    assert {issue.code for issue in report.issues} >= {"preserve_exact_violation"}


def test_placeholder_name_and_multiplicity_are_enforced(governed_units) -> None:
    policy, units = governed_units
    unit = next(item for item in units if item.key == "chat.session.completed")
    proposal = TranslationProposal(
        key=unit.key,
        source_text=unit.source_text,
        source_hash=unit.source_hash,
        candidate_chinese="第 {number} 次揭示已完成。",
        confidence=0.8,
        status=ProposalStatus.MACHINE_PROPOSED,
        provider="glm",
        protected_terms=unit.protected_terms,
        generated_by=GenerationOrigin.LOCAL_AI,
    )
    report = LocalizationValidator(policy).validate_batch([unit], [proposal])
    assert report.valid is False
    assert {issue.code for issue in report.issues} >= {"target_placeholders_mismatch"}


def test_machine_proposal_cannot_claim_approved_status(governed_units) -> None:
    _, units = governed_units
    unit = units[0]
    payload = {
        "key": unit.key,
        "source_text": unit.source_text,
        "source_hash": unit.source_hash,
        "candidate_chinese": "测试",
        "confidence": 0.5,
        "status": "approved",
    }
    with pytest.raises(ValidationError):
        TranslationProposal.model_validate(payload)


def test_proposal_cannot_claim_manual_generation_origin(governed_units) -> None:
    _, units = governed_units
    unit = units[0]
    payload = {
        "key": unit.key,
        "source_text": unit.source_text,
        "source_hash": unit.source_hash,
        "candidate_chinese": unit.source_text,
        "confidence": 0.5,
        "provider": "human",
        "generated_by": "manual",
    }
    with pytest.raises(ValidationError):
        TranslationProposal.model_validate(payload)


def test_proposal_requires_explicit_consistent_provenance(governed_units) -> None:
    _, units = governed_units
    unit = units[0]
    base = {
        "key": unit.key,
        "source_text": unit.source_text,
        "source_hash": unit.source_hash,
        "candidate_chinese": unit.source_text,
        "confidence": 0.5,
    }

    with pytest.raises(ValidationError):
        TranslationProposal.model_validate(base)
    with pytest.raises(ValidationError):
        TranslationProposal.model_validate(
            {
                **base,
                "status": "machine_reviewed",
                "provider": "glm",
                "generated_by": "deterministic",
            }
        )


class FakeStructuredGLM:
    def __init__(self) -> None:
        self.calls = []

    def generate_structured(self, prompt, response_model, **kwargs):
        self.calls.append((prompt, kwargs))
        payload = json.loads(prompt.split("BATCH_INPUT:\n", 1)[1])
        proposals = []
        for unit in payload["units"]:
            target = (
                unit["localization_source"]
                if unit["action"] in {"preserve_exact", "preserve_english_only"}
                else "放下问题牌"
            )
            proposals.append(
                {
                    "key": unit["key"],
                    "candidate_chinese": target,
                    "confidence": 0.88,
                    "status": "machine_proposed",
                    "rationale": "简洁 UI 文案并保留受保护术语。",
                }
            )
        return response_model.model_validate({"proposals": proposals})


class FakeStructuredGLMWithoutStatus(FakeStructuredGLM):
    def generate_structured(self, prompt, response_model, **kwargs):
        payload = json.loads(prompt.split("BATCH_INPUT:\n", 1)[1])
        unit = payload["units"][0]
        return response_model.model_validate(
            {
                "proposals": [
                    {
                        "key": unit["key"],
                        "candidate_chinese": "提交问题",
                        "confidence": 0.8,
                        "rationale": "简洁 UI 文案。",
                    }
                ]
            }
        )


def test_glm_proposer_uses_structured_bounded_ui_only_payload(governed_units) -> None:
    _, units = governed_units
    selected = [
        next(item for item in units if item.key == "chat.composer.submit"),
        next(item for item in units if item.key == "chat.header.effect"),
    ]
    fake = FakeStructuredGLM()
    proposals = GLMLocalizationProposer(fake, batch_size=1).propose(selected)

    assert len(fake.calls) == 2
    assert [proposal.status for proposal in proposals] == [
        ProposalStatus.MACHINE_PROPOSED,
        ProposalStatus.MACHINE_PROPOSED,
    ]
    assert all(proposal.generated_by == GenerationOrigin.LOCAL_AI for proposal in proposals)
    assert proposals[1].candidate_chinese == "Effect"
    assert proposals[0].source_text == selected[0].source_text
    assert proposals[0].source_hash == selected[0].source_hash
    for prompt, kwargs in fake.calls:
        payload = json.loads(prompt.split("BATCH_INPUT:\n", 1)[1])
        assert set(payload["units"][0]) == {
            "key",
            "source_text",
            "localization_source",
            "source_hash",
            "current_target",
            "classification",
            "action",
            "placeholders",
            "protected_terms",
            "required_target_terms",
            "context",
        }
        assert kwargs["temperature"] == 0.0
        assert kwargs["thinking_enabled"] is False
    protected_payload = json.loads(fake.calls[1][0].split("BATCH_INPUT:\n", 1)[1])
    assert protected_payload["units"][0]["required_target_terms"] == [
        {"canonical": "Effect", "minimum_occurrences": 1}
    ]


def test_glm_response_may_omit_fixed_machine_status(governed_units) -> None:
    _, units = governed_units
    unit = next(item for item in units if item.key == "chat.composer.submit")

    proposal = GLMLocalizationProposer(FakeStructuredGLMWithoutStatus()).propose([unit])[0]

    assert proposal.status == ProposalStatus.MACHINE_PROPOSED
    assert proposal.provider == "glm"
    assert proposal.generated_by == GenerationOrigin.LOCAL_AI


def test_glm_preflight_rejects_stale_protected_term_metadata(governed_units) -> None:
    policy, units = governed_units
    unit = next(item for item in units if item.key == "chat.turn.studyingDescription")
    stale = unit.model_copy(update={"protected_terms": ()})
    fake = FakeStructuredGLM()

    with pytest.raises(LocalizationProposalError, match="stale or violates"):
        GLMLocalizationProposer(
            fake,
            validator=LocalizationValidator(policy),
        ).propose([stale])

    assert fake.calls == []


def test_glm_postflight_rejects_noncanonical_source_variant(governed_units) -> None:
    policy, units = governed_units
    unit = next(item for item in units if item.key == "chat.turn.studyingDescription")

    with pytest.raises(LocalizationProposalError, match="Cognitive Mechanism"):
        GLMLocalizationProposer(
            FakeStructuredGLM(),
            validator=LocalizationValidator(policy),
        ).propose([unit])


def test_scan_cli_writes_local_artifacts_without_model_call(tmp_path: Path) -> None:
    protected_paths = (
        PROJECT_ROOT / "frontend/src/lib/i18n/messages.ts",
        PROJECT_ROOT / "localization/glossary.en-zh.yaml",
        PROJECT_ROOT / "localization/do-not-translate.yaml",
        PROJECT_ROOT / "localization/translation-memory.jsonl",
    )
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in protected_paths
    }
    result = main(
        [
            "scan",
            "--run-id",
            "test-localization-scan",
            "--output-root",
            str(tmp_path),
        ]
    )
    run = tmp_path / "test-localization-scan"
    # The human-authorized v2 runtime catalog now passes the same deterministic
    # governance checks that previously surfaced legacy terminology drift.
    assert result == 0
    assert (run / "manifest.json").is_file()
    assert (run / "source-units.jsonl").is_file()
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["llm_invoked"] is False
    assert manifest["runtime_modified"] is False
    assert manifest["translation_memory_modified"] is False
    assert manifest["external_translation_platform_used"] is False
    assert manifest["status"] == "completed"
    assert manifest["validation_passed"] is True
    assert manifest["completed_at"] == manifest["finished_at"]
    assert set(manifest["artifacts"]) >= {
        "inventory.json",
        "source-units.jsonl",
        "validation-report.json",
        "summary.md",
    }
    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in protected_paths
    }
    assert after == before


def test_resume_isolates_stale_proposal_and_failure_artifacts(tmp_path: Path) -> None:
    run = tmp_path / "test-localization-resume"
    run.mkdir()
    stale_names = (
        "manifest.json",
        "proposals.jsonl",
        "review-queue.jsonl",
        "failure.json",
    )
    for name in stale_names:
        (run / name).write_text("stale\n", encoding="utf-8")

    result = main(
        [
            "scan",
            "--run-id",
            "test-localization-resume",
            "--output-root",
            str(tmp_path),
            "--resume",
        ]
    )

    assert result == 0
    assert (run / "manifest.json").is_file()
    assert not (run / "proposals.jsonl").exists()
    assert not (run / "review-queue.jsonl").exists()
    assert not (run / "failure.json").exists()
    archived_attempts = list((run / "attempts").iterdir())
    assert len(archived_attempts) == 1
    for name in stale_names:
        assert (archived_attempts[0] / name).read_text(encoding="utf-8") == "stale\n"


def test_resume_isolation_rolls_back_a_partial_move(tmp_path: Path, monkeypatch) -> None:
    run = tmp_path / "rollback-run"
    run.mkdir()
    first = run / "manifest.json"
    second = run / "proposals.jsonl"
    first.write_text("manifest\n", encoding="utf-8")
    second.write_text("proposals\n", encoding="utf-8")
    real_replace = __import__("os").replace
    calls = 0

    def fail_second_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated isolation failure")
        real_replace(source, destination)

    monkeypatch.setattr("localization.pipeline.artifacts.os.replace", fail_second_replace)

    with pytest.raises(ArtifactError):
        isolate_existing_run_artifacts(run)

    assert first.read_text(encoding="utf-8") == "manifest\n"
    assert second.read_text(encoding="utf-8") == "proposals\n"


def test_validate_manifest_marks_supplied_provenance_unknown(
    tmp_path: Path, governed_units
) -> None:
    _, units = governed_units
    unit = next(item for item in units if item.key == "chat.composer.submit")
    proposal = TranslationProposal(
        key=unit.key,
        source_text=unit.source_text,
        source_hash=unit.source_hash,
        candidate_chinese=unit.current_target or unit.source_text,
        confidence=0.8,
        status=ProposalStatus.MACHINE_PROPOSED,
        provider="glm",
        protected_terms=unit.protected_terms,
        generated_by=GenerationOrigin.LOCAL_AI,
    )
    proposal_file = tmp_path / "proposal.jsonl"
    proposal_file.write_text(
        json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    result = main(
        [
            "validate",
            "--input",
            str(proposal_file),
            "--run-id",
            "test-supplied-validation",
            "--output-root",
            str(tmp_path),
        ]
    )

    manifest = json.loads(
        (tmp_path / "test-supplied-validation" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert result == 0
    assert manifest["status"] == "completed"
    assert manifest["validation_scope"] == "supplied_proposal_records"
    assert manifest["proposal_provenance"] == "supplied_input_unverified"
    assert manifest["external_translation_platform_used"] is None
    assert manifest["human_translator_used"] is None


def test_validate_cli_rejects_empty_proposal_file(tmp_path: Path) -> None:
    proposal_file = tmp_path / "empty.jsonl"
    proposal_file.write_text("", encoding="utf-8")

    result = main(
        [
            "validate",
            "--input",
            str(proposal_file),
            "--run-id",
            "test-empty-validation",
            "--output-root",
            str(tmp_path),
        ]
    )

    assert result == 2
    assert not (tmp_path / "test-empty-validation").exists()


def test_safe_error_message_redacts_authorization_bearer_secret() -> None:
    message = _safe_error_message(RuntimeError("Authorization: Bearer supersecret"))

    assert "supersecret" not in message
    assert "[redacted]" in message


def test_translation_memory_remains_empty() -> None:
    records = [
        json.loads(line)
        for line in (PROJECT_ROOT / "localization/translation-memory.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert records[0]["translation_unit_count"] == 0
    assert len(records) == 1


def _catalog_sha256(inventory: dict) -> str:
    payload = json.dumps(
        inventory["catalogs"]["enUS"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _machine_proposal(unit, candidate: str | None = None) -> TranslationProposal:
    return TranslationProposal(
        key=unit.key,
        source_text=unit.source_text,
        source_hash=unit.source_hash,
        candidate_chinese=candidate or unit.source_text,
        rationale="GLM machine candidate for assembly testing.",
        confidence=0.91,
        status=ProposalStatus.MACHINE_PROPOSED,
        provider="glm",
        protected_terms=unit.protected_terms,
        generated_by=GenerationOrigin.LOCAL_AI,
    )


def _write_completed_propose_run(
    root: Path,
    run_id: str,
    proposals: list[TranslationProposal],
    *,
    canonical_catalog_sha256: str,
    status: str = "completed",
    command: str = "propose",
) -> Path:
    run = root / run_id
    run.mkdir()
    proposals_path = run / "proposals.jsonl"
    write_jsonl(proposals_path, proposals)
    payload = proposals_path.read_bytes()
    write_json(
        run / "manifest.json",
        {
            "schema_version": "1.0",
            "run_id": run_id,
            "command": command,
            "status": status,
            "source_locale": "en-US",
            "target_locale": "zh-CN",
            "canonical_catalog_sha256": canonical_catalog_sha256,
            "unit_count": len(proposals),
            "proposal_count": len(proposals),
            "validation_passed": True,
            "llm_invoked": True,
            "llm_provider": "GLM",
            "llm_model": "glm-test",
            "external_translation_platform_used": False,
            "human_translator_used": False,
            "runtime_modified": False,
            "translation_memory_modified": False,
            "artifacts": {
                "proposals.jsonl": {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bytes": len(payload),
                }
            },
        },
    )
    return run


def test_assemble_overlays_later_verified_run_and_records_lineage(
    tmp_path: Path, inventory, governed_units
) -> None:
    _, units = governed_units
    unit = next(item for item in units if item.key == "chat.composer.submit")
    catalog_hash = _catalog_sha256(inventory)
    first = _write_completed_propose_run(
        tmp_path,
        "verified-propose-one",
        [_machine_proposal(unit, "提交问题")],
        canonical_catalog_sha256=catalog_hash,
    )
    second_proposal = _machine_proposal(unit, "放下问题牌")
    second = _write_completed_propose_run(
        tmp_path,
        "verified-propose-two",
        [second_proposal],
        canonical_catalog_sha256=catalog_hash,
    )

    result = main(
        [
            "assemble",
            "--input-run",
            str(first),
            "--input-run",
            str(second),
            "--run-id",
            "assembled-candidates",
            "--output-root",
            str(tmp_path),
        ]
    )

    run = tmp_path / "assembled-candidates"
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    proposals = [
        TranslationProposal.model_validate(record)
        for record in read_jsonl(run / "proposals.jsonl")
    ]
    assert result == 0
    assert proposals == [second_proposal]
    assert manifest["command"] == "assemble"
    assert manifest["status"] == "completed"
    assert manifest["validation_passed"] is True
    assert manifest["overridden_keys"] == [unit.key]
    assert manifest["override_events"] == [
        {
            "key": unit.key,
            "replaced_run_id": "verified-propose-one",
            "replacement_run_id": "verified-propose-two",
        }
    ]
    assert [item["run_id"] for item in manifest["upstream_runs"]] == [
        "verified-propose-one",
        "verified-propose-two",
    ]
    assert all(item["manifest_sha256"] for item in manifest["upstream_runs"])
    assert all(item["proposals_sha256"] for item in manifest["upstream_runs"])
    assert manifest["runtime_modified"] is False
    assert manifest["translation_memory_modified"] is False
    assert manifest["human_translator_used"] is False
    assert manifest["external_translation_platform_used"] is False
    assert {
        "localization/pipeline/cli.py",
        "localization/pipeline/models.py",
        "localization/pipeline/policy.py",
        "localization/pipeline/validator.py",
        "localization/pipeline/proposer.py",
        "localization/pipeline/workflow.py",
        "localization/pipeline/artifacts.py",
        "localization/pipeline/assets.py",
        "localization/pipeline/inventory.py",
        "frontend/scripts/localization-inventory.mjs",
    } <= set(manifest["pipeline_implementation"])


def test_assemble_rejects_manifest_to_proposals_hash_mismatch(
    tmp_path: Path, inventory, governed_units
) -> None:
    _, units = governed_units
    unit = next(item for item in units if item.key == "chat.composer.submit")
    input_run = _write_completed_propose_run(
        tmp_path,
        "tampered-propose",
        [_machine_proposal(unit)],
        canonical_catalog_sha256=_catalog_sha256(inventory),
    )
    with (input_run / "proposals.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("\n")

    result = main(
        [
            "assemble",
            "--input-run",
            str(input_run),
            "--run-id",
            "must-not-exist",
            "--output-root",
            str(tmp_path),
        ]
    )

    assert result == 2
    assert not (tmp_path / "must-not-exist").exists()


@pytest.mark.parametrize(
    ("status", "command"),
    [("failed", "propose"), ("completed", "validate")],
)
def test_assemble_rejects_noncompleted_or_nonpropose_input(
    tmp_path: Path, inventory, governed_units, status: str, command: str
) -> None:
    _, units = governed_units
    unit = next(item for item in units if item.key == "chat.composer.submit")
    input_run = _write_completed_propose_run(
        tmp_path,
        f"bad-{status}-{command}",
        [_machine_proposal(unit)],
        canonical_catalog_sha256=_catalog_sha256(inventory),
        status=status,
        command=command,
    )

    with pytest.raises(ArtifactError):
        assemble_verified_runs(
            [input_run],
            units,
            canonical_catalog_sha256=_catalog_sha256(inventory),
        )


def test_assemble_rejects_stale_catalog_identity(
    tmp_path: Path, inventory, governed_units
) -> None:
    _, units = governed_units
    unit = next(item for item in units if item.key == "chat.composer.submit")
    stale = _machine_proposal(unit).model_copy(
        update={
            "source_text": unit.source_text + " stale",
            "source_hash": hashlib.sha256(
                (unit.source_text + " stale").encode("utf-8")
            ).hexdigest(),
        }
    )
    input_run = _write_completed_propose_run(
        tmp_path,
        "stale-propose",
        [stale],
        canonical_catalog_sha256=_catalog_sha256(inventory),
    )

    with pytest.raises(ArtifactError, match="stale source text"):
        assemble_verified_runs(
            [input_run],
            units,
            canonical_catalog_sha256=_catalog_sha256(inventory),
        )


def test_assemble_complete_catalog_gate_fails_before_output_creation(
    tmp_path: Path, inventory, governed_units
) -> None:
    _, units = governed_units
    input_run = _write_completed_propose_run(
        tmp_path,
        "partial-propose",
        [_machine_proposal(units[0])],
        canonical_catalog_sha256=_catalog_sha256(inventory),
    )

    result = main(
        [
            "assemble",
            "--input-run",
            str(input_run),
            "--require-complete-catalog",
            "--run-id",
            "incomplete-assembly",
            "--output-root",
            str(tmp_path),
        ]
    )

    assert result == 2
    assert not (tmp_path / "incomplete-assembly").exists()
