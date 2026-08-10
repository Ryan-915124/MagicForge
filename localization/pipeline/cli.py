"""Command-line interface for MagicForge's local-first localization pipeline."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Sequence

from pydantic import ValidationError

from localization.pipeline.artifacts import (
    ArtifactError,
    default_run_id,
    fingerprint_run_artifacts,
    isolate_existing_run_artifacts,
    prepare_run_directory,
    read_jsonl,
    utc_timestamp,
    write_json,
    write_jsonl,
    write_text,
)
from localization.pipeline.assets import LocalizationAssetError, load_localization_asset_manifest
from localization.pipeline.assembler import assemble_verified_runs
from localization.pipeline.inventory import InventoryError, collect_frontend_inventory
from localization.pipeline.models import TranslationProposal
from localization.pipeline.policy import PolicyConfigurationError, load_policy_index
from localization.pipeline.validator import LocalizationValidator
from localization.pipeline.workflow import (
    audit_existing_targets,
    build_source_units,
    govern_hardcoded_candidates,
    render_summary,
    select_units,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "localization/runs"
MODULES = ("language", "shell", "chat", "evidence", "knowledge", "dashboard", "research", "shared")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m localization.pipeline",
        description=(
            "Audit MagicForge localization locally and optionally ask the existing GLM "
            "adapter for unapproved zh-CN UI candidates."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="run a fully offline catalog and policy audit")
    _add_run_arguments(scan, prefix="localization-scan")
    scan.set_defaults(handler=_command_scan)

    propose = subparsers.add_parser(
        "propose",
        help="plan candidates locally; --use-glm is required before any model call",
    )
    _add_run_arguments(propose, prefix="localization-proposal")
    propose.add_argument("--key", action="append", default=[], help="message key to include; repeatable")
    propose.add_argument("--module", action="append", choices=MODULES, default=[], help="catalog namespace to include; repeatable")
    propose.add_argument(
        "--all-normal-ui",
        action="store_true",
        help="include every unit classified as ordinary/mixed UI copy",
    )
    propose.add_argument(
        "--use-glm",
        action="store_true",
        help="explicitly permit calls through MagicForge's existing GLM client",
    )
    propose.add_argument("--batch-size", type=int, default=8, help="GLM units per batch (1-16)")
    propose.set_defaults(handler=_command_propose)

    validate = subparsers.add_parser(
        "validate",
        help="validate a proposal JSONL file without calling a model or modifying runtime",
    )
    _add_run_arguments(validate, prefix="localization-validation")
    validate.add_argument("--input", type=Path, required=True, help="proposal JSONL to validate")
    validate.set_defaults(handler=_command_validate)

    assemble = subparsers.add_parser(
        "assemble",
        help="overlay completed verified propose runs into one unpublishable candidate run",
    )
    _add_run_arguments(assemble, prefix="localization-assembly")
    assemble.add_argument(
        "--input-run",
        type=Path,
        action="append",
        required=True,
        help="completed propose run directory; repeat in overlay order (later wins)",
    )
    assemble.add_argument(
        "--require-complete-catalog",
        action="store_true",
        help="fail unless the final proposal keys exactly equal the current catalog",
    )
    assemble.set_defaults(handler=_command_assemble)

    quality_review = subparsers.add_parser(
        "quality-review",
        help=(
            "semantically review selected candidates from a verified assembly; "
            "--use-glm is required before any model call"
        ),
    )
    _add_run_arguments(quality_review, prefix="localization-quality-review")
    quality_review.add_argument(
        "--input-run",
        type=Path,
        required=True,
        help="completed verified assemble or quality-review candidate run",
    )
    quality_review.add_argument(
        "--key",
        action="append",
        default=[],
        help="message key to review; repeatable",
    )
    quality_review.add_argument(
        "--keys-file",
        type=Path,
        action="append",
        default=[],
        help="newline-delimited message keys to review; repeatable",
    )
    quality_review.add_argument(
        "--changed-only",
        action="store_true",
        help="review every candidate that differs from the current runtime target",
    )
    quality_review.add_argument(
        "--use-glm",
        action="store_true",
        help="explicitly permit second-pass calls through the existing GLM client",
    )
    quality_review.add_argument(
        "--batch-size",
        type=int,
        default=8,
        help="GLM review units per batch (1-16)",
    )
    quality_review.set_defaults(handler=_command_quality_review)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        ArtifactError,
        InventoryError,
        LocalizationAssetError,
        PolicyConfigurationError,
        ValidationError,
        ValueError,
    ) as exc:
        print(f"Localization pipeline error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # GLM adapter failures are intentionally summarized without prompts,
        # environment values, or credentials.
        from llm.glm_client import GLMAPIError, GLMConfigurationError
        from localization.pipeline.proposer import LocalizationProposalError
        from localization.pipeline.reviewer import LocalizationQualityReviewError

        if isinstance(exc, (GLMAPIError, GLMConfigurationError)):
            print(f"Localization GLM error: {_safe_error_message(exc)}", file=sys.stderr)
            return 3
        if isinstance(exc, LocalizationProposalError):
            print(f"Localization proposal error: {exc}", file=sys.stderr)
            return 2
        if isinstance(exc, LocalizationQualityReviewError):
            print(f"Localization quality-review error: {exc}", file=sys.stderr)
            return 2
        raise


def _add_run_arguments(parser: argparse.ArgumentParser, *, prefix: str) -> None:
    parser.add_argument("--run-id", default=None, help="stable lowercase run identifier")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--resume", action="store_true", help="reuse an existing run directory")
    parser.add_argument("--node", default="node", help="Node.js executable for the AST inventory")
    parser.set_defaults(run_prefix=prefix)


def _command_scan(args: argparse.Namespace) -> int:
    inventory, policy, units, validator = _load_workspace(args.node)
    _, report = audit_existing_targets(units, validator)
    run_id, run_directory = _prepare_run(args)
    manifest = _manifest(
        run_id=run_id,
        command="scan",
        inventory=inventory,
        policy_schema=policy.schema_version,
        unit_count=len(units),
        llm_invoked=False,
    )
    _write_common_artifacts(run_directory, manifest, inventory, units, report)
    write_text(
        run_directory / "summary.md",
        render_summary(
            title="MagicForge Localization Scan",
            inventory=inventory,
            units=units,
            report=report,
            proposal_count=0,
            llm_invoked=False,
        ),
    )
    _finalize_manifest(run_directory, manifest, validation_passed=report.valid)
    _print_result(run_directory, report.valid, len(units), 0)
    return 0 if report.valid else 2


def _command_propose(args: argparse.Namespace) -> int:
    inventory, policy, units, validator = _load_workspace(args.node)
    selected = select_units(
        units,
        keys=args.key,
        modules=args.module,
        all_normal_ui=args.all_normal_ui,
    )
    if not selected:
        raise ValueError("select at least one --key, --module, or --all-normal-ui unit")

    run_id, run_directory = _prepare_run(args)
    _, baseline_report = audit_existing_targets(selected, validator)
    manifest = _manifest(
        run_id=run_id,
        command="propose",
        inventory=inventory,
        policy_schema=policy.schema_version,
        unit_count=len(selected),
        llm_invoked=False,
    )
    _write_common_artifacts(
        run_directory,
        manifest,
        inventory,
        selected,
        baseline_report,
        inventory_filename="inventory-summary.json",
    )

    if not args.use_glm:
        write_text(
            run_directory / "summary.md",
            render_summary(
                title="MagicForge Localization Proposal Plan",
                inventory=inventory,
                units=selected,
                report=baseline_report,
                proposal_count=0,
                llm_invoked=False,
            )
            + "\nRun the same command with `--use-glm` to explicitly permit candidate generation.\n",
        )
        _finalize_manifest(
            run_directory,
            manifest,
            validation_passed=baseline_report.valid,
        )
        _print_result(run_directory, baseline_report.valid, len(selected), 0)
        return 0 if baseline_report.valid else 2

    from app.config import Settings
    from llm.glm_client import GLMClient
    from localization.pipeline.proposer import GLMLocalizationProposer

    settings = Settings.from_env()
    llm = GLMClient(
        api_key=settings.glm_api_key,
        model=settings.glm_model,
        timeout_seconds=settings.glm_timeout_seconds,
        max_retries=settings.glm_max_retries,
    )
    proposer = GLMLocalizationProposer(
        llm,
        batch_size=args.batch_size,
        validator=validator,
    )
    try:
        proposals = proposer.propose(selected)
    except Exception as exc:
        failed_at = utc_timestamp()
        write_json(
            run_directory / "failure.json",
            {
                "failed_at": failed_at,
                "stage": "glm_candidate_generation",
                "error_type": type(exc).__name__,
                "message": _safe_error_message(exc),
                "prompt_or_credentials_recorded": False,
            },
        )
        manifest.update(
            {
                "llm_invoked": True,
                "llm_provider": "GLM",
                "llm_model": settings.glm_model,
                "failed_at": failed_at,
            }
        )
        _finalize_manifest(run_directory, manifest, status="failed")
        raise

    report = validator.validate_batch(selected, proposals)
    manifest.update(
        {
            "llm_invoked": True,
            "llm_provider": "GLM",
            "llm_model": settings.glm_model,
            "proposal_count": len(proposals),
            "runtime_modified": False,
            "translation_memory_modified": False,
        }
    )
    write_jsonl(run_directory / "proposals.jsonl", proposals)
    write_json(run_directory / "validation-report.json", report)
    write_jsonl(
        run_directory / "review-queue.jsonl",
        _review_records(proposals, report),
    )
    write_text(
        run_directory / "summary.md",
        render_summary(
            title="MagicForge GLM Localization Proposals",
            inventory=inventory,
            units=selected,
            report=report,
            proposal_count=len(proposals),
            llm_invoked=True,
        ),
    )
    _finalize_manifest(run_directory, manifest, validation_passed=report.valid)
    _print_result(run_directory, report.valid, len(selected), len(proposals))
    return 0 if report.valid else 2


def _command_validate(args: argparse.Namespace) -> int:
    input_path = args.input.resolve()
    if not input_path.is_file():
        raise ValueError(f"proposal file not found: {input_path}")
    raw_proposals = read_jsonl(input_path)
    if not raw_proposals:
        raise ValueError("proposal file must contain at least one proposal record")
    proposals = [TranslationProposal.model_validate(item) for item in raw_proposals]
    inventory, policy, units, validator = _load_workspace(args.node)
    selected = select_units(units, keys=[proposal.key for proposal in proposals])
    report = validator.validate_batch(selected, proposals)
    run_id, run_directory = _prepare_run(args)
    manifest = _manifest(
        run_id=run_id,
        command="validate",
        inventory=inventory,
        policy_schema=policy.schema_version,
        unit_count=len(selected),
        llm_invoked=False,
    )
    manifest["validated_input"] = str(input_path)
    manifest["validated_input_sha256"] = hashlib.sha256(input_path.read_bytes()).hexdigest()
    manifest["validation_scope"] = "supplied_proposal_records"
    manifest["proposal_provenance"] = "supplied_input_unverified"
    manifest["external_translation_platform_used"] = None
    manifest["human_translator_used"] = None
    _write_common_artifacts(
        run_directory,
        manifest,
        inventory,
        selected,
        report,
        inventory_filename="inventory-summary.json",
    )
    write_jsonl(run_directory / "proposals.jsonl", proposals)
    write_jsonl(run_directory / "review-queue.jsonl", _review_records(proposals, report))
    write_text(
        run_directory / "summary.md",
        render_summary(
            title="MagicForge Localization Proposal Validation",
            inventory=inventory,
            units=selected,
            report=report,
            proposal_count=len(proposals),
            llm_invoked=False,
        ),
    )
    _finalize_manifest(run_directory, manifest, validation_passed=report.valid)
    _print_result(run_directory, report.valid, len(selected), len(proposals))
    return 0 if report.valid else 2


def _command_assemble(args: argparse.Namespace) -> int:
    inventory, policy, units, validator = _load_workspace(args.node)
    canonical_catalog_sha256 = _canonical_catalog_sha256(inventory)
    result = assemble_verified_runs(
        args.input_run,
        units,
        canonical_catalog_sha256=canonical_catalog_sha256,
        require_complete_catalog=args.require_complete_catalog,
    )
    proposals = list(result.proposals)
    selected = list(result.source_units)
    report = validator.validate_batch(selected, proposals)

    run_id, run_directory = _prepare_run(args)
    manifest = _manifest(
        run_id=run_id,
        command="assemble",
        inventory=inventory,
        policy_schema=policy.schema_version,
        unit_count=len(selected),
        llm_invoked=False,
    )
    manifest.update(
        {
            "proposal_count": len(proposals),
            "proposal_provenance": "assembled_verified_glm_propose_runs",
            "upstream_llm_invoked": True,
            "upstream_runs": [
                upstream.lineage_record() for upstream in result.upstream_runs
            ],
            "overlay_order": [upstream.run_id for upstream in result.upstream_runs],
            "overridden_keys": list(result.overridden_keys),
            "override_events": list(result.override_events),
            "require_complete_catalog": args.require_complete_catalog,
            "complete_catalog": len(selected) == len(units),
            "runtime_modified": False,
            "translation_memory_modified": False,
            "external_translation_platform_used": False,
            "human_translator_used": False,
            "pipeline_implementation": _pipeline_implementation_fingerprints(),
        }
    )
    _write_common_artifacts(
        run_directory,
        manifest,
        inventory,
        selected,
        report,
        inventory_filename="inventory-summary.json",
    )
    write_jsonl(run_directory / "proposals.jsonl", proposals)
    write_jsonl(run_directory / "review-queue.jsonl", _review_records(proposals, report))
    write_text(
        run_directory / "summary.md",
        _render_assembly_summary(
            inventory=inventory,
            units=selected,
            report=report,
            proposals=proposals,
            result=result,
            require_complete_catalog=args.require_complete_catalog,
            catalog_unit_count=len(units),
        ),
    )
    _finalize_manifest(run_directory, manifest, validation_passed=report.valid)
    _print_result(run_directory, report.valid, len(selected), len(proposals))
    return 0 if report.valid else 2


def _command_quality_review(args: argparse.Namespace) -> int:
    from localization.pipeline.reviewer import (
        GLMLocalizationQualityReviewer,
        load_verified_assembly_run,
        read_review_keys,
        select_review_candidates,
    )

    inventory, policy, units, validator = _load_workspace(args.node)
    canonical_catalog_sha256 = _canonical_catalog_sha256(inventory)
    verified_run = load_verified_assembly_run(
        args.input_run,
        units,
        canonical_catalog_sha256=canonical_catalog_sha256,
    )
    units_by_key = {unit.key: unit for unit in units}
    assembly_units = [units_by_key[proposal.key] for proposal in verified_run.proposals]
    assembly_report = validator.validate_batch(
        assembly_units,
        list(verified_run.proposals),
    )
    if not assembly_report.valid:
        issue = next(item for item in assembly_report.issues if item.severity.value == "error")
        raise ArtifactError(
            f"verified quality-review assembly failed current validation for {issue.key!r}: "
            f"{issue.code}"
        )
    requested_keys = list(args.key)
    key_files: list[dict[str, Any]] = []
    for keys_file in args.keys_file:
        resolved = keys_file.resolve()
        file_keys = read_review_keys(resolved)
        requested_keys.extend(file_keys)
        payload = resolved.read_bytes()
        key_files.append(
            {
                "path": str(resolved),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "key_count": len(file_keys),
            }
        )
    selected, input_proposals = select_review_candidates(
        verified_run,
        units,
        keys=requested_keys,
        changed_only=args.changed_only,
    )
    baseline_report = validator.validate_batch(selected, input_proposals)
    if not baseline_report.valid:
        issue = next(item for item in baseline_report.issues if item.severity.value == "error")
        raise ArtifactError(
            f"verified quality-review input failed current validation for {issue.key!r}: "
            f"{issue.code}"
        )

    run_id, run_directory = _prepare_run(args)
    manifest = _manifest(
        run_id=run_id,
        command="quality-review",
        inventory=inventory,
        policy_schema=policy.schema_version,
        unit_count=len(selected),
        llm_invoked=False,
    )
    manifest.update(
        {
            "input_proposal_count": len(input_proposals),
            "proposal_provenance": "quality_review_over_verified_candidate_run",
            "input_candidate_run": verified_run.lineage_record(),
            # Retained for compatibility with v0.1 quality-review manifests.
            "input_assembly": verified_run.lineage_record(),
            "review_selection": {
                "explicit_keys": list(dict.fromkeys(args.key)),
                "keys_files": key_files,
                "changed_only": args.changed_only,
                "selected_key_count": len(selected),
                "selected_keys": [unit.key for unit in selected],
            },
            "runtime_modified": False,
            "translation_memory_modified": False,
            "external_translation_platform_used": False,
            "human_translator_used": False,
            "pipeline_implementation": _pipeline_implementation_fingerprints(),
        }
    )
    _write_common_artifacts(
        run_directory,
        manifest,
        inventory,
        list(selected),
        baseline_report,
        inventory_filename="inventory-summary.json",
    )

    if not args.use_glm:
        write_text(
            run_directory / "summary.md",
            _render_quality_review_summary(
                units=list(selected),
                report=baseline_report,
                input_run=verified_run,
                decisions=[],
                model_calls=False,
            )
            + "\nRun the same command with `--use-glm` to explicitly permit semantic review.\n",
        )
        _finalize_manifest(
            run_directory,
            manifest,
            validation_passed=baseline_report.valid,
        )
        _print_result(run_directory, baseline_report.valid, len(selected), 0)
        return 0

    from app.config import Settings
    from llm.glm_client import GLMClient

    settings = Settings.from_env()
    llm = GLMClient(
        api_key=settings.glm_api_key,
        model=settings.glm_model,
        timeout_seconds=settings.glm_timeout_seconds,
        max_retries=settings.glm_max_retries,
    )
    reviewer = GLMLocalizationQualityReviewer(
        llm,
        validator=validator,
        batch_size=args.batch_size,
    )
    try:
        result = reviewer.review(selected, input_proposals)
    except Exception as exc:
        failed_at = utc_timestamp()
        write_json(
            run_directory / "failure.json",
            {
                "failed_at": failed_at,
                "stage": "glm_localization_quality_review",
                "error_type": type(exc).__name__,
                "message": _safe_error_message(exc),
                "prompt_or_credentials_recorded": False,
            },
        )
        manifest.update(
            {
                "llm_invoked": True,
                "llm_provider": "GLM",
                "llm_model": settings.glm_model,
                "failed_at": failed_at,
            }
        )
        _finalize_manifest(run_directory, manifest, status="failed")
        raise

    proposals = list(result.proposals)
    decisions = list(result.decisions)
    report = validator.validate_batch(selected, proposals)
    reviewed_by_key = {proposal.key: proposal for proposal in proposals}
    input_by_key = {proposal.key: proposal for proposal in verified_run.proposals}
    final_proposals = [
        reviewed_by_key.get(unit.key, input_by_key[unit.key])
        for unit in units
        if unit.key in input_by_key
    ]
    final_units = [units_by_key[proposal.key] for proposal in final_proposals]
    final_report = validator.validate_batch(final_units, final_proposals)
    if not final_report.valid:
        issue = next(item for item in final_report.issues if item.severity.value == "error")
        overlay_error = ArtifactError(
            f"quality-review full overlay failed validation for {issue.key!r}: {issue.code}"
        )
        failed_at = utc_timestamp()
        write_json(
            run_directory / "failure.json",
            {
                "failed_at": failed_at,
                "stage": "quality_review_full_overlay_validation",
                "error_type": type(overlay_error).__name__,
                "message": _safe_error_message(overlay_error),
                "prompt_or_credentials_recorded": False,
            },
        )
        manifest.update(
            {
                "llm_invoked": True,
                "llm_provider": "GLM",
                "llm_model": settings.glm_model,
                "failed_at": failed_at,
            }
        )
        _finalize_manifest(run_directory, manifest, status="failed")
        raise overlay_error
    catalog_keys = {unit.key for unit in units}
    final_keys = {proposal.key for proposal in final_proposals}
    counts = Counter(decision.disposition.value for decision in decisions)
    normalization_counts = Counter(
        decision.normalization.value
        for decision in decisions
        if decision.normalization is not None
    )
    manifest.update(
        {
            "llm_invoked": True,
            "llm_provider": "GLM",
            "llm_model": settings.glm_model,
            "quality_review_count": len(decisions),
            "proposal_count": len(proposals),
            "final_proposal_count": len(final_proposals),
            "full_catalog_overlay": final_keys == catalog_keys,
            "final_validation_passed": final_report.valid,
            "review_counts": {
                "keep": counts["keep"],
                "revise": counts["revise"],
                "needs_human_review": counts["needs_human_review"],
            },
            "review_normalization_count": sum(normalization_counts.values()),
            "review_normalization_counts": dict(normalization_counts),
            "runtime_modified": False,
            "translation_memory_modified": False,
        }
    )
    write_jsonl(run_directory / "review-decisions.jsonl", decisions)
    write_jsonl(run_directory / "proposals.jsonl", proposals)
    write_jsonl(run_directory / "final-proposals.jsonl", final_proposals)
    write_json(run_directory / "validation-report.json", report)
    write_json(run_directory / "final-validation-report.json", final_report)
    write_jsonl(
        run_directory / "review-queue.jsonl",
        _quality_review_records(decisions, proposals, report),
    )
    write_text(
        run_directory / "summary.md",
        _render_quality_review_summary(
            units=list(selected),
            report=report,
            input_run=verified_run,
            decisions=decisions,
            model_calls=True,
            final_proposal_count=len(final_proposals),
            full_catalog_overlay=final_keys == catalog_keys,
        ),
    )
    _finalize_manifest(run_directory, manifest, validation_passed=report.valid)
    _print_result(run_directory, report.valid, len(selected), len(proposals))
    return 0 if report.valid else 2


def _load_workspace(node_binary: str):
    inventory = collect_frontend_inventory(PROJECT_ROOT, node_binary=node_binary)
    asset_manifest = load_localization_asset_manifest(PROJECT_ROOT)
    policy = load_policy_index()
    if asset_manifest["schema_version"] != policy.schema_version:
        raise LocalizationAssetError(
            "Translation Memory and terminology policy must use the same schema_version"
        )
    if (
        asset_manifest["source_locale"] != "en-US"
        or asset_manifest["target_locale"] != "zh-CN"
    ):
        raise LocalizationAssetError(
            "Translation Memory must declare the en-US to zh-CN locale pair"
        )
    inventory["localizationAssets"] = asset_manifest
    inventory["hardcodedGovernance"] = govern_hardcoded_candidates(inventory, policy)
    validator = LocalizationValidator(policy)
    units = build_source_units(inventory, policy)
    return inventory, policy, units, validator


def _prepare_run(args: argparse.Namespace) -> tuple[str, Path]:
    run_id = args.run_id or default_run_id(args.run_prefix)
    output_root = args.output_root.resolve()
    run_directory = prepare_run_directory(output_root, run_id, resume=args.resume)
    if args.resume:
        isolate_existing_run_artifacts(run_directory)
    return run_id, run_directory


def _manifest(
    *,
    run_id: str,
    command: str,
    inventory: dict[str, Any],
    policy_schema: str,
    unit_count: int,
    llm_invoked: bool,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "command": command,
        "status": "started",
        "created_at": utc_timestamp(),
        "source_locale": "en-US",
        "target_locale": "zh-CN",
        "canonical_catalog": "frontend/src/lib/i18n/messages.ts#enUS",
        "canonical_catalog_sha256": _canonical_catalog_sha256(inventory),
        "policy_schema_version": policy_schema,
        "localization_assets": inventory.get("localizationAssets", {}),
        "unit_count": unit_count,
        "llm_invoked": llm_invoked,
        "external_translation_platform_used": False,
        "runtime_modified": False,
        "translation_memory_modified": False,
        "human_translator_used": False,
    }


def _finalize_manifest(
    run_directory: Path,
    manifest: dict[str, Any],
    *,
    status: str = "completed",
    validation_passed: bool | None = None,
) -> None:
    finished_at = utc_timestamp()
    manifest["status"] = status
    manifest["finished_at"] = finished_at
    if status == "completed":
        manifest["completed_at"] = finished_at
    if validation_passed is not None:
        manifest["validation_passed"] = validation_passed
    manifest["artifacts"] = fingerprint_run_artifacts(run_directory)
    # The manifest is the run-level commit record and is deliberately written last.
    write_json(run_directory / "manifest.json", manifest)


def _write_common_artifacts(
    run_directory: Path,
    manifest: dict[str, Any],
    inventory: dict[str, Any],
    units: list[Any],
    report: Any,
    *,
    inventory_filename: str = "inventory.json",
) -> None:
    write_json(run_directory / "manifest.json", manifest)
    inventory_value = inventory if inventory_filename == "inventory.json" else {
        "ok": inventory["ok"],
        "schemaVersion": inventory["schemaVersion"],
        "summary": inventory["summary"],
        "catalogValidation": inventory["catalogValidation"],
        "hardcodedCandidates": inventory["hardcodedCandidates"],
    }
    write_json(run_directory / inventory_filename, inventory_value)
    write_jsonl(run_directory / "source-units.jsonl", units)
    write_jsonl(
        run_directory / "hardcoded-candidates.jsonl",
        inventory.get("hardcodedGovernance", []),
    )
    write_json(run_directory / "validation-report.json", report)


def _review_records(
    proposals: list[TranslationProposal],
    report: Any,
) -> list[dict[str, Any]]:
    issues_by_key: dict[str, list[dict[str, Any]]] = {}
    for issue in report.issues:
        if issue.key:
            issues_by_key.setdefault(issue.key, []).append(issue.model_dump(mode="json"))
    return [
        {
            "proposal": proposal.model_dump(mode="json"),
            "validation": {
                "valid": not any(
                    issue["severity"] == "error"
                    for issue in issues_by_key.get(proposal.key, [])
                ),
                "issues": issues_by_key.get(proposal.key, []),
            },
            "runtime_applied": False,
            "translation_memory_written": False,
            "governance_decision": "pending",
        }
        for proposal in proposals
    ]


def _quality_review_records(
    decisions: list[Any],
    proposals: list[TranslationProposal],
    report: Any,
) -> list[dict[str, Any]]:
    """Build a non-approving queue from second-pass semantic decisions."""

    issues_by_key: dict[str, list[dict[str, Any]]] = {}
    for issue in report.issues:
        if issue.key:
            issues_by_key.setdefault(issue.key, []).append(issue.model_dump(mode="json"))
    proposals_by_key = {proposal.key: proposal for proposal in proposals}
    return [
        {
            "quality_review": decision.model_dump(mode="json"),
            "result_proposal": proposals_by_key[decision.key].model_dump(mode="json"),
            "validation": {
                "valid": not any(
                    issue["severity"] == "error"
                    for issue in issues_by_key.get(decision.key, [])
                ),
                "issues": issues_by_key.get(decision.key, []),
            },
            "human_review_required_before_publication": True,
            "runtime_applied": False,
            "translation_memory_written": False,
            "governance_decision": "pending",
        }
        for decision in decisions
    ]


def _canonical_catalog_sha256(inventory: dict[str, Any]) -> str:
    canonical_payload = json.dumps(
        inventory["catalogs"]["enUS"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def _pipeline_implementation_fingerprints() -> dict[str, dict[str, Any]]:
    relative_paths = (
        "localization/pipeline/cli.py",
        "localization/pipeline/models.py",
        "localization/pipeline/policy.py",
        "localization/pipeline/validator.py",
        "localization/pipeline/proposer.py",
        "localization/pipeline/workflow.py",
        "localization/pipeline/artifacts.py",
        "localization/pipeline/assets.py",
        "localization/pipeline/inventory.py",
        "localization/pipeline/assembler.py",
        "localization/pipeline/reviewer.py",
        "localization/prompts/ui-quality-reviewer-v1.md",
        "frontend/scripts/localization-inventory.mjs",
    )
    output: dict[str, dict[str, Any]] = {}
    for relative_path in relative_paths:
        path = PROJECT_ROOT / relative_path
        if not path.is_file():
            raise ArtifactError(
                f"localization pipeline implementation file not found: {relative_path}"
            )
        payload = path.read_bytes()
        output[relative_path] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
    return output


def _render_assembly_summary(
    *,
    inventory: dict[str, Any],
    units: list[Any],
    report: Any,
    proposals: list[TranslationProposal],
    result: Any,
    require_complete_catalog: bool,
    catalog_unit_count: int,
) -> str:
    summary = render_summary(
        title="MagicForge Assembled GLM Localization Candidates",
        inventory=inventory,
        units=units,
        report=report,
        proposal_count=len(proposals),
        llm_invoked=False,
    )
    lines = [
        summary.rstrip(),
        "",
        "## Verified upstream lineage",
        "",
        f"- Completed propose runs: {len(result.upstream_runs)}",
        f"- Overlay events: {len(result.override_events)}",
        f"- Unique overridden keys: {len(result.overridden_keys)}",
        f"- Catalog coverage: {len(units)}/{catalog_unit_count}",
        f"- Complete catalog required: {'yes' if require_complete_catalog else 'no'}",
        "- Model calls made by assembly: none",
        "- Upstream provider: GLM only",
        "- Human translators used: no",
        "",
        "Input order is authoritative: a later verified run replaces an earlier candidate for the same key. Exact manifest and proposal SHA-256 lineage is recorded in `manifest.json`.",
        "",
        "## Non-publication boundary",
        "",
        "This assembled set remains machine-proposed review material. Runtime messages and Translation Memory are unchanged, and this command cannot apply, publish, approve, or promote candidates.",
    ]
    return "\n".join(lines) + "\n"


def _render_quality_review_summary(
    *,
    units: list[Any],
    report: Any,
    input_run: Any,
    decisions: list[Any],
    model_calls: bool,
    final_proposal_count: int | None = None,
    full_catalog_overlay: bool | None = None,
) -> str:
    dispositions = Counter(decision.disposition.value for decision in decisions)
    normalizations = Counter(
        decision.normalization.value
        for decision in decisions
        if decision.normalization is not None
    )
    issue_types = Counter(
        issue.value for decision in decisions for issue in decision.issue_types
    )
    lines = [
        "# MagicForge Localization Semantic Quality Review",
        "",
        f"- Verified input candidate run: `{input_run.run_id}` ({input_run.command})",
        f"- Candidate artifact: `{input_run.proposals_artifact}`",
        f"- Input proposals SHA-256: `{input_run.proposals_sha256}`",
        f"- Selected UI candidates: {len(units)}",
        f"- GLM semantic review invoked: {'yes' if model_calls else 'no'}",
        f"- Deterministic validation: {'PASS' if report.valid else 'FAIL'}",
        f"- Validation errors: {report.error_count}",
        f"- Validation warnings: {report.warning_count}",
    ]
    if final_proposal_count is not None:
        lines.extend(
            [
                f"- Unified final candidates: {final_proposal_count}",
                "- Full canonical catalog overlay: "
                + ("yes" if full_catalog_overlay else "no"),
            ]
        )
    if decisions:
        lines.extend(
            [
                f"- Keep: {dispositions['keep']}",
                f"- Revise: {dispositions['revise']}",
                f"- Needs human review: {dispositions['needs_human_review']}",
                f"- Fail-safe normalizations: {sum(normalizations.values())}",
                "",
                "## Reported semantic issue types",
                "",
            ]
        )
        if issue_types:
            lines.extend(
                f"- `{issue}`: {count}" for issue, count in issue_types.most_common()
            )
        else:
            lines.append("- None")
        if normalizations:
            lines.extend(
                [
                    "",
                    "## Fail-safe normalizations",
                    "",
                    *(
                        f"- `{normalization}`: {count}"
                        for normalization, count in normalizations.most_common()
                    ),
                ]
            )
    lines.extend(
        [
            "",
            "## Governance boundary",
            "",
            "This is GLM-generated second-pass review material. A `keep` recommendation is not human approval. Every revision remains `machine_proposed`; runtime messages and Translation Memory are unchanged.",
        ]
    )
    return "\n".join(lines) + "\n"


def _print_result(run_directory: Path, valid: bool, units: int, proposals: int) -> None:
    print(f"Run: {run_directory}")
    print(f"Status: {'PASS' if valid else 'FAIL'}")
    print(f"Units: {units}")
    print(f"Proposals: {proposals}")


def _safe_error_message(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    message = re.sub(
        r"(?i)\bauthorization\s*[:=]?\s*(?:bearer\s+)?[^\s,;]+",
        "authorization [redacted]",
        message,
    )
    message = re.sub(
        r"(?i)(api[_-]?key|bearer|token|secret|password)\s*[:=]?\s*[^\s,;]+",
        r"\1 [redacted]",
        message,
    )
    return message[:600] or type(exc).__name__
