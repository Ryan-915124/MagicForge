"""Build an isolated Bootstrap input from an audited generic Source ledger.

This is the narrow bridge between the reusable acquisition staging boundary and
the legacy Bootstrap v0.2 semantic pipeline.  Preparing the input is offline
and deterministic.  It creates no approval, does not call GLM, and cannot write
Qdrant.  GLM execution requires the explicit ``--execute-glm`` flag and still
runs the existing Bootstrap runner with Qdrant disabled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from knowledge.evidence import ClaimRole, KnowledgeOrigin
from knowledge.governance import (
    ExtractionPermission,
    SensitiveInformationLevel,
    StoragePermission,
)
from research.acquisition.generic_staging import verify_generic_staging
from research.acquisition.staging import AcquisitionStagingError
from research.citation.manager import CitationManager
from research.citation.models import CitationRecord, PeerReviewStatus
from research.models import ContentAccess, ResearchCandidate, SourceCategory
from research.review.workflow_models import SourceVersionCommand


INPUT_SCHEMA_VERSION = "bootstrap-generic-input-0.1"
POLICY_SCHEMA_VERSION = "bootstrap-source-claim-policy-0.1"
SUPPORTED_AUDIT_SCHEMAS = {
    "magicforge-practitioner-exact-content-audit-0.1",
    "magicforge-practitioner-exact-content-audit-0.2",
}
DEFAULT_LEDGER_SHA256 = (
    "42faf80fc4839adf6c350ed3460194ac1e8955eb56034719615121183715eede"
)
DEFAULT_STAGING_ROOT = Path(
    "research/runs/production-source-acquisition-003/staging/practitioner"
)
DEFAULT_AUDIT = Path(
    "research/runs/production-source-acquisition-003/reports/"
    "practitioner-exact-content-audit.json"
)
DEFAULT_OUTPUT = Path("research/runs/bootstrap-003")
DEFAULT_ACADEMIC_LEDGER_SHA256 = (
    "b0a4476be6781e2e3db3a481db6bdeb13f654292b099bc2e9b85359b0f1abad3"
)
DEFAULT_ACADEMIC_STAGING_ROOT = Path(
    "research/runs/production-source-acquisition-003/staging/academic"
)
DEFAULT_ACADEMIC_AUDIT = Path(
    "research/runs/production-source-acquisition-003/reports/"
    "academic-full-text-audit.json"
)
DEFAULT_ACADEMIC_INDEPENDENT_AUDIT = Path(
    "research/runs/production-source-acquisition-003/reports/"
    "academic-independent-audit.json"
)

_TRACKING_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}
_ALLOWED_AUDIT_ROLES = {
    ClaimRole.EXPERT_OPINION,
    ClaimRole.CONTEXT_ONLY,
}
_ALLOWED_ACADEMIC_ROLES = {
    ClaimRole.RESULT,
    ClaimRole.METHOD,
    ClaimRole.BACKGROUND,
    ClaimRole.HYPOTHESIS,
    ClaimRole.DISCUSSION,
    ClaimRole.CONTEXT_ONLY,
}
_EXACT_CONTENT_CHANNELS = {
    ContentAccess.WEB_EXTRACT,
    ContentAccess.FULL_TEXT,
    ContentAccess.PDF_TEXT,
}
_EXPANSION_RUN_PATTERN = re.compile(r"bootstrap-(\d{3})")
_FOUNDATION_RUN_IDS = frozenset({"bootstrap-001", "bootstrap-002"})


class BootstrapInputError(ValueError):
    """A fail-closed generic-ledger to Bootstrap conversion error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _expansion_run_id(output_root: Path) -> str:
    """Accept only isolated post-foundation Bootstrap run directories."""

    output_root = Path(output_root)
    run_id = output_root.name
    match = _EXPANSION_RUN_PATTERN.fullmatch(run_id)
    if match is None:
        raise BootstrapInputError(
            "invalid_output_identity",
            "Bootstrap expansion output must be named bootstrap-NNN.",
        )
    if int(match.group(1)) < 3:
        raise BootstrapInputError(
            "authoritative_run_is_immutable",
            "Bootstrap foundation runs 001 and 002 cannot be modified.",
        )
    resolved_root = output_root.resolve()
    lexical_ancestors = {path.name for path in output_root.parents}
    resolved_ancestors = {path.name for path in resolved_root.parents}
    if _FOUNDATION_RUN_IDS & (lexical_ancestors | resolved_ancestors):
        raise BootstrapInputError(
            "authoritative_run_is_immutable",
            "Bootstrap expansion output cannot be nested below foundation runs 001 or 002.",
        )
    if output_root.is_symlink() or resolved_root.name != run_id:
        raise BootstrapInputError(
            "invalid_output_identity",
            "Bootstrap expansion output cannot alias another run directory.",
        )
    return run_id


def _isolated_output_descendant(
    path: Path,
    output_root: Path,
    *,
    code: str = "output_path_not_isolated",
) -> Path:
    """Resolve one output path and reject aliases that escape its run root."""

    resolved_root = Path(output_root).resolve()
    resolved_path = Path(path).resolve()
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        raise BootstrapInputError(
            code,
            "Bootstrap artifact path must remain inside its isolated output run.",
        )
    return resolved_path


def prepare_bootstrap_input(
    staging_root: Path,
    ledger_sha256: str,
    audit_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Verify one strict practitioner audit and emit deterministic run inputs."""

    return _prepare_lane_bootstrap_input(
        staging_root,
        ledger_sha256,
        audit_path,
        output_root,
        lane="practitioner",
    )


def prepare_academic_bootstrap_input(
    staging_root: Path,
    ledger_sha256: str,
    audit_path: Path,
    independent_audit_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Verify both academic audits and emit an independent immutable input."""

    return _prepare_lane_bootstrap_input(
        staging_root,
        ledger_sha256,
        audit_path,
        output_root,
        lane="academic",
        independent_audit_path=independent_audit_path,
    )


def _prepare_lane_bootstrap_input(
    staging_root: Path,
    ledger_sha256: str,
    audit_path: Path,
    output_root: Path,
    *,
    lane: str,
    independent_audit_path: Path | None = None,
) -> dict[str, Any]:
    if lane not in {"practitioner", "academic"}:
        raise BootstrapInputError("unsupported_lane", "Unsupported Bootstrap lane.")

    staging_root = Path(staging_root)
    audit_path = Path(audit_path)
    independent_audit_path = (
        Path(independent_audit_path) if independent_audit_path is not None else None
    )
    output_root = Path(output_root)
    run_id = _expansion_run_id(output_root)

    try:
        verification = verify_generic_staging(staging_root, ledger_sha256)
    except AcquisitionStagingError as exc:
        raise BootstrapInputError(exc.code, str(exc)) from exc
    ledger_path = staging_root / "ledgers" / f"{ledger_sha256}.json"
    ledger = _read_object(ledger_path, "invalid_ledger")
    audit = _read_object(audit_path, "invalid_semantic_audit")
    independent_audit = (
        _read_object(independent_audit_path, "invalid_independent_audit")
        if independent_audit_path is not None
        else None
    )
    if lane == "practitioner":
        audit_by_url = _validate_practitioner_audit(audit)
        expected_category = SourceCategory.PRACTITIONER
        expected_origin = KnowledgeOrigin.EXPERT_PRACTICE
        content_name = "practitioner.json"
    else:
        if independent_audit is None:
            raise BootstrapInputError(
                "independent_audit_required",
                "Academic preparation requires an independent audit.",
            )
        audit_by_url = _validate_academic_audits(
            audit, independent_audit, ledger_sha256
        )
        expected_category = SourceCategory.ACADEMIC
        expected_origin = KnowledgeOrigin.SCIENTIFIC_EVIDENCE
        content_name = "academic.json"

    candidates: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    content_records: dict[str, dict[str, Any]] = {}
    source_policies: dict[str, dict[str, Any]] = {}

    prepared_entries = [
        entry
        for entry in ledger.get("entries", [])
        if (entry.get("source_submission") or {}).get("status") == "prepared"
    ]
    prepared_urls = {_canonical_url(str(entry["canonical_url"])) for entry in prepared_entries}
    if prepared_urls != set(audit_by_url):
        raise BootstrapInputError(
            "audit_coverage_mismatch",
            "Strict audit records must match the prepared ledger Source set exactly.",
        )

    for entry in sorted(prepared_entries, key=lambda item: str(item["candidate_id"])):
        submission = entry["source_submission"]
        payload_path = _safe_payload_path(staging_root, str(submission["payload_file"]))
        wrapper = _read_object(payload_path, "invalid_source_payload")
        try:
            command = SourceVersionCommand.model_validate(wrapper["payload"])
        except (KeyError, ValueError) as exc:
            raise BootstrapInputError(
                "invalid_source_payload", "Staged Source payload is invalid."
            ) from exc
        url = _canonical_url(command.citation.url)
        policy = audit_by_url.get(url)
        if policy is None:
            raise BootstrapInputError(
                "audit_record_missing", f"No strict audit record covers {url}."
            )
        _validate_source_payload(
            entry,
            wrapper,
            command,
            policy,
            lane=lane,
        )

        candidate = ResearchCandidate(
            title=command.citation.title,
            url=url,
            authors=command.citation.authors,
            published_year=command.citation.year,
            venue=command.citation.venue,
            doi=command.citation.doi,
            snippet="",
            content=None,
            content_access=ContentAccess.SEARCH_SNIPPET,
            source_category=command.source_category,
            provenance=command.citation.provenance,
        )
        if candidate.id != str(entry["candidate_id"]) or candidate.id != str(
            wrapper.get("candidate_id")
        ):
            raise BootstrapInputError(
                "candidate_identity_mismatch",
                f"Candidate identity does not match exact Source payload for {url}.",
            )

        accessed_at = max(
            (item.retrieved_at for item in candidate.provenance),
            default=datetime(1970, 1, 1, tzinfo=UTC),
        )
        citation = CitationManager().validate(
            CitationRecord(
                source_candidate_id=candidate.id,
                source_category=candidate.source_category,
                title=candidate.title,
                authors=candidate.authors,
                year=candidate.published_year,
                venue=candidate.venue,
                volume=command.citation.volume,
                issue=command.citation.issue,
                pages=command.citation.pages,
                doi=candidate.doi,
                url=candidate.url,
                accessed_at=accessed_at,
                peer_review_status=command.citation.peer_review_status,
                provenance=candidate.provenance,
            )
        )
        candidates.append(candidate.model_dump(mode="json"))
        source_records.append(
            {
                "source_id": citation.id,
                "candidate_id": candidate.id,
                "source_category": expected_category.value,
                "knowledge_origin": expected_origin.value,
                "source_role": policy["classification"],
                "citation": citation.model_dump(mode="json"),
                "access_check": {
                    "state": f"strict_{lane}_audit_passed",
                    "url": candidate.url,
                    "content_sha256": wrapper["content_sha256"],
                    "notes": policy["verification"],
                },
                "source_approval": {
                    "status": "bootstrap_pending_human_review",
                    "human_verified": False,
                    "reason": policy["restriction"],
                },
            }
        )
        content_records[candidate.url] = {
            "candidate_id": candidate.id,
            "title": candidate.title,
            "content": command.content,
            "content_access": command.content_access.value,
            "content_sha256": wrapper["content_sha256"],
            "provider": entry["exact_content"]["provider"],
            "retrieved_at": entry["exact_content"]["retrieved_at"],
            "bootstrap_generated": False,
            "human_verified": False,
        }
        source_policies[candidate.id] = {
            "candidate_id": candidate.id,
            "canonical_url": candidate.url,
            "source_category": expected_category.value,
            "knowledge_origin": expected_origin.value,
            "allowed_claim_roles": policy["claim_roles_allowed"],
            "classification": policy["classification"],
            "restriction": policy["restriction"],
            "content_sha256": wrapper["content_sha256"],
            "doi": _normalize_doi(command.citation.doi or "") or None,
            "published_year": command.citation.year,
            "scientific_assertions": (
                "context_only"
                if lane == "practitioner"
                else "classify_from_claim_role_and_locator"
            ),
            "human_verified": False,
        }

    # Each audited batch is immutable and content addressed.  A newer ledger
    # never overwrites an earlier prepared input under the same Bootstrap run.
    input_root = output_root / "input_batches" / ledger_sha256
    _isolated_output_descendant(input_root, output_root)
    output_values: dict[Path, object] = {
        input_root / "candidates" / "academic-candidates.json": (
            candidates if lane == "academic" else []
        ),
        input_root / "candidates" / "practitioner-candidates.json": (
            candidates if lane == "practitioner" else []
        ),
        input_root / "sources" / "verified-academic-sources.json": (
            source_records if lane == "academic" else []
        ),
        input_root
        / "sources"
        / "access-checked-practitioner-sources.json": (
            source_records if lane == "practitioner" else []
        ),
        input_root / "content" / content_name: {"sources": content_records},
        input_root / "policies" / "source-claim-policies.json": {
            "schema_version": POLICY_SCHEMA_VERSION,
            "ledger_sha256": ledger_sha256,
            "audit_sha256": _sha256_bytes(audit_path.read_bytes()),
            "independent_audit_sha256": (
                _sha256_bytes(independent_audit_path.read_bytes())
                if independent_audit_path is not None
                else None
            ),
            "sources": source_policies,
        },
    }
    files: list[dict[str, Any]] = []
    for path, value in output_values.items():
        _isolated_output_descendant(path, output_root)
        data = _json_bytes(value)
        _write_immutable(path, data)
        files.append(
            {
                "path": path.relative_to(output_root).as_posix(),
                "sha256": _sha256_bytes(data),
                "bytes": len(data),
            }
        )

    manifest_without_hash: dict[str, Any] = {
        "schema_version": INPUT_SCHEMA_VERSION,
        "run_id": run_id,
        "mode": "dry_run",
        "source_ledger_sha256": ledger_sha256,
        "semantic_audit_sha256": _sha256_bytes(audit_path.read_bytes()),
        "independent_audit_sha256": (
            _sha256_bytes(independent_audit_path.read_bytes())
            if independent_audit_path is not None
            else None
        ),
        "summary": {
            "ledger_entries_prepared": len(prepared_entries),
            "strict_audit_records": len(audit_by_url),
            "bootstrap_candidates_prepared": len(candidates),
            "source_lane": lane,
            "knowledge_origin": expected_origin.value,
        },
        "constraints": {
            "glm_called": False,
            "database_modified": False,
            "approval_created": False,
            "qdrant_modified": False,
            "production_collection_modified": False,
            "human_verified": False,
            "review_status_after_extraction": "bootstrap_generated",
        },
        "generic_staging_verification": verification,
        "files": sorted(files, key=lambda item: item["path"]),
    }
    manifest_hash = _canonical_hash(manifest_without_hash)
    manifest = {**manifest_without_hash, "manifest_sha256": manifest_hash}
    manifest_path = input_root / "preparation-manifest.json"
    _isolated_output_descendant(manifest_path, output_root)
    _write_immutable(manifest_path, _json_bytes(manifest))
    return {
        "status": "dry_run_prepared",
        "run_id": run_id,
        "input_root": input_root.as_posix(),
        "content_bundle": (input_root / "content" / content_name).as_posix(),
        "manifest_file": manifest_path.as_posix(),
        "manifest_sha256": manifest_hash,
        "candidates_prepared": len(candidates),
        "glm_called": False,
        "qdrant_modified": False,
    }


def execute_prepared_bootstrap(
    preparation: dict[str, Any],
    *,
    output_root: Path,
    max_source_characters: int = 8_000,
    workers: int = 2,
    request_interval_seconds: float = 0.0,
) -> dict[str, Any]:
    """Run GLM extraction explicitly, while keeping every Qdrant writer off."""

    return execute_prepared_bootstrap_batches(
        [preparation],
        output_root=output_root,
        max_source_characters=max_source_characters,
        workers=workers,
        request_interval_seconds=request_interval_seconds,
    )


def execute_prepared_bootstrap_batches(
    preparations: Sequence[dict[str, Any]],
    *,
    output_root: Path,
    max_source_characters: int = 8_000,
    workers: int = 2,
    request_interval_seconds: float = 0.0,
) -> dict[str, Any]:
    """Execute one combined GLM run from independently audited input batches."""

    from research.bootstrap.runner import _verify_prepared_input_manifest, run

    if not preparations:
        raise BootstrapInputError(
            "preparation_required", "At least one prepared input batch is required."
        )
    run_id = _expansion_run_id(Path(output_root))
    validated_preparations = [
        _validate_execution_preparation(
            item,
            output_root=Path(output_root),
            run_id=run_id,
            manifest_verifier=_verify_prepared_input_manifest,
        )
        for item in preparations
    ]
    input_roots = [item["input_root"] for item in validated_preparations]
    if len({path.resolve() for path in input_roots}) != len(input_roots):
        raise BootstrapInputError(
            "duplicate_prepared_batch", "Prepared input batches must be unique."
        )
    report = run(
        source_run=input_roots[0],
        additional_source_runs=input_roots[1:],
        output=Path(output_root),
        content_bundle=validated_preparations[0]["content_bundle"],
        additional_content_bundles=[
            item["content_bundle"] for item in validated_preparations[1:]
        ],
        ingest_qdrant=False,
        max_source_characters=max_source_characters,
        workers=workers,
        force_reprocess=False,
        request_interval_seconds=request_interval_seconds,
    )
    if report.get("run_id") != run_id:
        raise BootstrapInputError(
            "execution_run_identity_mismatch",
            "Bootstrap execution report does not match the isolated output run.",
        )
    expected_lineages = [item["lineage"] for item in validated_preparations]
    actual_lineages = report.get("input_lineage", [])
    if sorted(_canonical_hash(item) for item in expected_lineages) != sorted(
        _canonical_hash(item) for item in actual_lineages
    ):
        raise BootstrapInputError(
            "execution_lineage_missing",
            "Bootstrap execution result did not preserve every Source/audit lineage.",
        )
    if report.get("qdrant_ingestion_attempted") is not False:
        raise BootstrapInputError(
            "qdrant_boundary_violation",
            f"{output_root.name} adapter must never initiate Qdrant ingestion.",
        )
    if (report.get("safety") or {}).get("production_collection_touched") is not False:
        raise BootstrapInputError(
            "production_boundary_violation",
            f"{output_root.name} must never touch the Production collection.",
        )
    return report


def _validate_execution_preparation(
    preparation: dict[str, Any],
    *,
    output_root: Path,
    run_id: str,
    manifest_verifier: Any,
) -> dict[str, Any]:
    """Bind an execution descriptor to one verified immutable input batch."""

    if (
        preparation.get("status") != "dry_run_prepared"
        or preparation.get("glm_called") is not False
        or preparation.get("qdrant_modified") is not False
    ):
        raise BootstrapInputError(
            "unsafe_preparation_state",
            "Only offline, Qdrant-free prepared inputs may execute.",
        )
    if preparation.get("run_id") != run_id:
        raise BootstrapInputError(
            "preparation_run_identity_mismatch",
            "Prepared input and execution output must name the same Bootstrap run.",
        )

    try:
        input_root = Path(str(preparation["input_root"]))
        manifest_file = Path(str(preparation["manifest_file"]))
        content_bundle = Path(str(preparation["content_bundle"]))
    except KeyError as exc:
        raise BootstrapInputError(
            "preparation_descriptor_missing",
            "Prepared execution descriptor is incomplete.",
        ) from exc

    resolved_input = _isolated_output_descendant(
        input_root,
        output_root,
        code="preparation_path_not_isolated",
    )
    resolved_manifest = manifest_file.resolve()
    expected_manifest = (resolved_input / "preparation-manifest.json").resolve()
    if resolved_manifest != expected_manifest:
        raise BootstrapInputError(
            "preparation_manifest_binding_mismatch",
            "Prepared descriptor does not reference its own immutable manifest.",
        )
    manifest = _read_object(expected_manifest, "invalid_preparation_manifest")
    ledger_sha256 = str(manifest.get("source_ledger_sha256") or "")
    expected_input = (
        Path(output_root).resolve() / "input_batches" / ledger_sha256
    ).resolve()
    if (
        resolved_input != expected_input
        or manifest.get("run_id") != run_id
        or preparation.get("manifest_sha256") != manifest.get("manifest_sha256")
    ):
        raise BootstrapInputError(
            "preparation_manifest_binding_mismatch",
            "Prepared descriptor identity does not match its immutable manifest.",
        )

    resolved_content = content_bundle.resolve()
    if (resolved_input / "content").resolve() not in resolved_content.parents:
        raise BootstrapInputError(
            "preparation_content_binding_mismatch",
            "Prepared content bundle must remain inside its immutable input batch.",
        )
    try:
        content_relative = resolved_content.relative_to(Path(output_root).resolve()).as_posix()
    except ValueError as exc:
        raise BootstrapInputError(
            "preparation_content_binding_mismatch",
            "Prepared content bundle escapes its isolated output run.",
        ) from exc
    committed_files = {
        str(item.get("path") or "")
        for item in manifest.get("files", [])
        if isinstance(item, dict)
    }
    if content_relative not in committed_files:
        raise BootstrapInputError(
            "preparation_content_binding_mismatch",
            "Prepared content bundle is not committed by its immutable manifest.",
        )

    try:
        verified_lineage = manifest_verifier(resolved_input, Path(output_root))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise BootstrapInputError(
            "invalid_preparation_manifest",
            "Prepared input manifest verification failed before GLM execution.",
        ) from exc
    expected_lineage = _preparation_lineage(manifest)
    if verified_lineage != expected_lineage:
        raise BootstrapInputError(
            "preparation_manifest_binding_mismatch",
            "Prepared input lineage does not match its immutable manifest.",
        )
    return {
        "input_root": resolved_input,
        "content_bundle": resolved_content,
        "lineage": expected_lineage,
    }


def _preparation_lineage(manifest: dict[str, Any]) -> dict[str, str]:
    lineage = {
        "source_ledger_sha256": str(manifest["source_ledger_sha256"]),
        "semantic_audit_sha256": str(manifest["semantic_audit_sha256"]),
        "preparation_manifest_sha256": str(manifest["manifest_sha256"]),
    }
    if manifest.get("independent_audit_sha256") is not None:
        lineage["independent_audit_sha256"] = str(
            manifest["independent_audit_sha256"]
        )
    return lineage


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare audited generic Sources for an isolated Bootstrap expansion run."
    )
    parser.add_argument(
        "--lane",
        choices=("practitioner", "academic", "combined"),
        default="practitioner",
        help="Prepare one lane or both independent lanes for a combined GLM run.",
    )
    parser.add_argument("--staging-root", type=Path, default=DEFAULT_STAGING_ROOT)
    parser.add_argument("--ledger-sha256", default=DEFAULT_LEDGER_SHA256)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument(
        "--academic-staging-root", type=Path, default=DEFAULT_ACADEMIC_STAGING_ROOT
    )
    parser.add_argument(
        "--academic-ledger-sha256", default=DEFAULT_ACADEMIC_LEDGER_SHA256
    )
    parser.add_argument("--academic-audit", type=Path, default=DEFAULT_ACADEMIC_AUDIT)
    parser.add_argument(
        "--academic-independent-audit",
        type=Path,
        default=DEFAULT_ACADEMIC_INDEPENDENT_AUDIT,
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--execute-glm",
        action="store_true",
        help="Explicitly call configured GLM; Qdrant remains disabled.",
    )
    parser.add_argument("--max-source-characters", type=int, default=8_000)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--request-interval-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)

    preparations = []
    if args.lane in {"practitioner", "combined"}:
        preparations.append(
            prepare_bootstrap_input(
                args.staging_root,
                args.ledger_sha256,
                args.audit,
                args.output,
            )
        )
    if args.lane in {"academic", "combined"}:
        preparations.append(
            prepare_academic_bootstrap_input(
                args.academic_staging_root,
                args.academic_ledger_sha256,
                args.academic_audit,
                args.academic_independent_audit,
                args.output,
            )
        )
    result: dict[str, Any]
    if len(preparations) == 1:
        result = preparations[0]
    else:
        result = {
            "status": "dry_run_prepared",
            "run_id": args.output.name,
            "batches": preparations,
            "candidates_prepared": sum(
                int(item["candidates_prepared"]) for item in preparations
            ),
            "glm_called": False,
            "qdrant_modified": False,
        }
    if args.execute_glm:
        result = execute_prepared_bootstrap_batches(
            preparations,
            output_root=args.output,
            max_source_characters=args.max_source_characters,
            workers=args.workers,
            request_interval_seconds=args.request_interval_seconds,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _validate_practitioner_audit(
    audit: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    schema = str(audit.get("schema_version") or "")
    if schema not in SUPPORTED_AUDIT_SCHEMAS:
        raise BootstrapInputError(
            "unsupported_audit_schema", "Unsupported practitioner semantic audit."
        )
    scope = audit.get("scope") or {}
    if any(scope.get(key) is not False for key in (
        "database_write",
        "glm_called",
        "approval_created",
        "qdrant_write",
    )):
        raise BootstrapInputError(
            "audit_boundary_violation", "Semantic audit contains an unsafe action marker."
        )
    policy = audit.get("policy") or {}
    common_policy_safe = (
        policy.get("knowledge_origin") == KnowledgeOrigin.EXPERT_PRACTICE.value
        and policy.get("peer_review_status") == PeerReviewStatus.NOT_APPLICABLE.value
    )
    if schema.endswith("0.1"):
        variant_safe = (
            policy.get("scientific_assertion_claim_role")
            == ClaimRole.CONTEXT_ONLY.value
            and policy.get("scientific_assertion_evidence_class") == "never_empirical"
            and policy.get("source_level_pass_does_not_equal_claim_approval") is True
        )
    else:
        variant_safe = (
            policy.get("source_eligibility_is_not_claim_approval") is True
            and policy.get("practitioner_source_is_never_empirical_evidence") is True
            and policy.get("public_availability_does_not_grant_redistribution") is True
            and policy.get("operational_or_restricted_secret_detail")
            == "blocked_from_exact_content_staging"
        )
    if not common_policy_safe or not variant_safe:
        raise BootstrapInputError(
            "unsafe_audit_policy", "Practitioner audit policy is not fail-closed."
        )

    output: dict[str, dict[str, Any]] = {}
    for raw in audit.get("records", []):
        if not isinstance(raw, dict):
            raise BootstrapInputError(
                "invalid_audit_record", "Audit records must be objects."
            )
        if raw.get("disposition") == "blocked_discovery_only":
            if (
                raw.get("exact_content_staged") is not False
                or raw.get("source_payload_allowed") is not False
                or not str(raw.get("blocked_reason") or "").strip()
            ):
                raise BootstrapInputError(
                    "unsafe_blocked_audit_record",
                    "Blocked audit records must remain discovery-only.",
                )
            continue
        if raw.get("disposition") != "eligible_exact_content":
            raise BootstrapInputError(
                "invalid_audit_record", "Unknown practitioner audit disposition."
            )
        url = _canonical_url(str(raw.get("url") or ""))
        if not url or url in output:
            raise BootstrapInputError(
                "duplicate_audit_locator", "Every audit record needs one unique URL."
            )
        try:
            roles = [ClaimRole(str(value)) for value in raw.get("claim_roles_allowed", [])]
        except ValueError as exc:
            raise BootstrapInputError(
                "invalid_audit_claim_role", "Audit contains an unsupported claim role."
            ) from exc
        if not roles or not set(roles).issubset(_ALLOWED_AUDIT_ROLES):
            raise BootstrapInputError(
                "unsafe_audit_claim_role",
                "Practitioner Sources may only allow expert_opinion/context_only.",
            )
        if raw.get("knowledge_origin", KnowledgeOrigin.EXPERT_PRACTICE.value) != (
            KnowledgeOrigin.EXPERT_PRACTICE.value
        ) or raw.get("empirical_evidence_allowed", False) is not False:
            raise BootstrapInputError(
                "unsafe_audit_epistemic_class",
                "Practitioner audit record cannot allow empirical evidence.",
            )
        if raw.get("sensitivity", SensitiveInformationLevel.CONTROLLED.value) != (
            SensitiveInformationLevel.CONTROLLED.value
        ):
            raise BootstrapInputError(
                "unsafe_audit_sensitivity",
                "Practitioner Bootstrap input is limited to controlled material.",
            )
        content_sha256 = str(raw.get("content_sha256") or "")
        if schema.endswith("0.2") and (
            len(content_sha256) != 64
            or any(value not in "0123456789abcdef" for value in content_sha256)
        ):
            raise BootstrapInputError(
                "audit_content_hash_missing",
                "v0.2 eligible audit records require an exact content SHA-256.",
            )
        if not str(raw.get("restriction") or "").strip():
            raise BootstrapInputError(
                "audit_reason_missing", "Audit records require a restriction."
            )
        output[url] = {
            **raw,
            "claim_roles_allowed": [role.value for role in roles],
            "classification": str(
                raw.get("classification") or "practitioner_exact_content"
            ),
            "verification": str(
                raw.get("verification")
                or "Exact content and checksum passed strict semantic audit."
            ),
        }
    if not output:
        raise BootstrapInputError("audit_empty", "Strict audit contains no eligible records.")
    return output


def _validate_academic_audits(
    audit: dict[str, Any],
    independent: dict[str, Any],
    ledger_sha256: str,
) -> dict[str, dict[str, Any]]:
    """Reconcile the full-text audit with its independent correction chain."""

    if audit.get("schema_version") != "magicforge-academic-full-text-audit-0.1":
        raise BootstrapInputError(
            "unsupported_academic_audit", "Unsupported academic full-text audit."
        )
    if independent.get("schema_version") != "magicforge-academic-independent-audit-0.1":
        raise BootstrapInputError(
            "unsupported_independent_audit", "Unsupported academic independent audit."
        )
    safety = audit.get("safety") or {}
    if any(
        safety.get(key) is not False
        for key in ("database_modified", "glm_called", "approval_created", "qdrant_modified")
    ):
        raise BootstrapInputError(
            "academic_audit_boundary_violation",
            "Academic audit contains an unsafe action marker.",
        )
    independent_scope = independent.get("scope") or {}
    if any(
        independent_scope.get(key) is not False
        for key in (
            "original_discovery_modified",
            "original_full_text_modified",
            "database_read_or_write",
            "glm_called",
            "approval_created",
            "qdrant_read_or_write",
        )
    ):
        raise BootstrapInputError(
            "independent_audit_boundary_violation",
            "Independent academic audit contains an unsafe action marker.",
        )
    accepted_count = len(audit.get("accepted") or [])
    full_summary = audit.get("summary") or {}
    independent_summary = independent.get("summary") or {}
    if (
        accepted_count < 1
        or full_summary.get("accepted_full_text") != accepted_count
        or independent_summary.get("ledger_integrity_pass") is not True
        or independent_summary.get("payload_hashes_verified") != accepted_count
        or independent_summary.get("complete_claim_extractable_full_text")
        != accepted_count
        or independent_summary.get("crossref_doi_resolved") != accepted_count
        or independent_summary.get("publication_year_matches") != accepted_count
        or independent_summary.get("peer_reviewed_final_or_accepted_journal_records")
        != accepted_count
        or independent_summary.get("knowledge_origin_boundary_pass") != accepted_count
        or independent_summary.get("eligible_for_source_review") != accepted_count
        or independent_summary.get("held_for_metadata_correction") != 0
    ):
        raise BootstrapInputError(
            "academic_audit_summary_mismatch",
            "Academic audit summary does not attest the complete authoritative wave.",
        )
    boundary = independent.get("evidence_boundary_assessment") or {}
    if (
        boundary.get("result") != "pass"
        or boundary.get("knowledge_origin") != KnowledgeOrigin.SCIENTIFIC_EVIDENCE.value
        or boundary.get("source_category") != SourceCategory.ACADEMIC.value
        or boundary.get("source_type") != "journal_article"
        or boundary.get("peer_review_status") != PeerReviewStatus.PEER_REVIEWED.value
        or boundary.get("requested_extraction_permission")
        != ExtractionPermission.NONE.value
        or boundary.get("requested_storage_permission") != StoragePermission.NONE.value
    ):
        raise BootstrapInputError(
            "unsafe_academic_evidence_boundary",
            "Independent audit does not preserve the academic evidence boundary.",
        )

    history = independent.get("ledger_history") or {}
    authoritative_history = history.get("authoritative") or {}
    if (
        independent.get("audited_ledger_sha256") != ledger_sha256
        or authoritative_history.get("ledger_sha256") != ledger_sha256
        or authoritative_history.get("integrity") != "pass"
        or independent.get("overall_verdict") != "pass_pending_human_source_review"
        or independent.get("blocking_action") is not None
    ):
        raise BootstrapInputError(
            "academic_correction_chain_mismatch",
            "Independent audit is not bound to the authoritative ledger.",
        )

    raw_corrections = audit.get("bibliographic_corrections") or []
    if not isinstance(raw_corrections, list) or any(
        not isinstance(item, dict) for item in raw_corrections
    ):
        raise BootstrapInputError(
            "academic_correction_chain_mismatch",
            "Academic bibliographic corrections must be a list of records.",
        )
    raw_issues = independent.get("resolved_metadata_issues")
    if raw_issues is None:
        singular_issue = independent.get("resolved_metadata_issue")
        raw_issues = [singular_issue] if isinstance(singular_issue, dict) else []
    if not isinstance(raw_issues, list) or any(
        not isinstance(item, dict) for item in raw_issues
    ):
        raise BootstrapInputError(
            "academic_correction_chain_mismatch",
            "Resolved academic metadata issues must be a list of records.",
        )
    corrections_by_doi = _academic_corrections_by_doi(
        raw_corrections,
        raw_issues,
        history=history,
        ledger_sha256=ledger_sha256,
    )

    raw_independent_records = [
        item for item in independent.get("records", []) if isinstance(item, dict)
    ]
    independent_records = {
        _normalize_doi(str(item.get("doi") or "")): item
        for item in raw_independent_records
    }
    if (
        len(raw_independent_records) != accepted_count
        or len(independent_records) != len(raw_independent_records)
        or "" in independent_records
    ):
        raise BootstrapInputError(
            "academic_independent_coverage_mismatch",
            "Independent academic audit requires one unique DOI record per accepted source.",
        )
    output: dict[str, dict[str, Any]] = {}
    accepted_dois: set[str] = set()
    for raw in audit.get("accepted", []):
        if not isinstance(raw, dict):
            raise BootstrapInputError(
                "invalid_academic_audit_record", "Academic audit records must be objects."
            )
        doi = _normalize_doi(str(raw.get("doi") or ""))
        url = _canonical_url(str(raw.get("url") or ""))
        title = str(raw.get("title") or "").strip()
        independent_record = independent_records.get(doi)
        full_text = raw.get("full_text") or {}
        if (
            not doi
            or not url
            or not title
            or doi in accepted_dois
            or raw.get("peer_review_status") != PeerReviewStatus.PEER_REVIEWED.value
            or raw.get("verdict") != "accepted_pending_human_source_review"
            or full_text.get("article_heading") is not True
            or full_text.get("reference_mentions", 0) < 1
            or full_text.get("complete_ending_preserved") is not True
            or full_text.get("truncation_detected") is not False
            or independent_record is None
            or _normalize_title_binding(str(independent_record.get("title") or ""))
            != _normalize_title_binding(title)
            or not str(independent_record.get("full_text") or "").startswith("pass")
            or independent_record.get("deduplication") != "unique"
            or not str(independent_record.get("boundary") or "").startswith("pass")
            or independent_record.get("pmcid") != raw.get("pmcid")
        ):
            raise BootstrapInputError(
                "academic_audit_record_mismatch",
                "Academic full-text and independent audit records do not agree for "
                f"DOI {doi or '<missing>'}.",
            )
        correction = corrections_by_doi.get(doi)
        is_corrected = correction is not None
        metadata_status = str(independent_record.get("metadata") or "")
        metadata_passed = (
            metadata_status == "pass"
            if not is_corrected
            else metadata_status == "pass_year_reconciled"
            or metadata_status.startswith("pass_year_corrected_to_")
        )
        expected_disposition = "eligible_for_source_review"
        if (
            not metadata_passed
            or independent_record.get("disposition") != expected_disposition
        ):
            raise BootstrapInputError(
                "academic_metadata_audit_mismatch",
                "Academic metadata disposition does not match correction state.",
            )
        accepted_dois.add(doi)
        output[url] = {
            **raw,
            "doi": doi,
            "classification": "peer_reviewed_full_text",
            "claim_roles_allowed": [
                ClaimRole.RESULT.value,
                ClaimRole.METHOD.value,
                ClaimRole.BACKGROUND.value,
                ClaimRole.HYPOTHESIS.value,
                ClaimRole.DISCUSSION.value,
                ClaimRole.CONTEXT_ONLY.value,
            ],
            "knowledge_origin": KnowledgeOrigin.SCIENTIFIC_EVIDENCE.value,
            "sensitivity": SensitiveInformationLevel.PUBLIC.value,
            "published_year": (
                int(correction["authoritative_value"])
                if correction is not None
                else None
            ),
            "content_sha256": (
                str(raw.get("content_sha256") or "")
                or str(independent_record.get("content_sha256") or "")
                or (
                    str(correction["issue"].get("exact_content_sha256") or "")
                    if correction is not None
                    else None
                )
            ),
            "content_characters": int(full_text.get("characters") or 0),
            "content_bytes": int(independent_record.get("content_bytes") or 0),
            "verification": (
                "Full article structure, DOI metadata, deduplication, rights boundary, "
                "and independent audit passed."
            ),
            "restriction": (
                "Evidence class must be derived from claim_role and exact locator; "
                "source type alone never establishes an empirical result."
            ),
        }
    if accepted_dois != set(independent_records):
        raise BootstrapInputError(
            "academic_audit_coverage_mismatch",
            "Full-text and independent academic audits must cover the same DOI set.",
        )
    if len(output) != int((audit.get("summary") or {}).get("accepted_full_text", -1)):
        raise BootstrapInputError(
            "academic_audit_count_mismatch", "Academic audit summary count is inconsistent."
        )
    return output


def _academic_corrections_by_doi(
    corrections: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    *,
    history: dict[str, Any],
    ledger_sha256: str,
) -> dict[str, dict[str, Any]]:
    """Validate zero or more audit-documented publication-year reconciliations."""

    issue_by_doi: dict[str, dict[str, Any]] = {}
    for issue in issues:
        doi = _normalize_doi(str(issue.get("doi") or ""))
        if not doi or doi in issue_by_doi:
            raise BootstrapInputError(
                "academic_correction_chain_mismatch",
                "Resolved metadata issues require unique DOI identities.",
            )
        issue_by_doi[doi] = issue

    superseded_raw = history.get("superseded")
    if isinstance(superseded_raw, dict):
        superseded_records = [superseded_raw]
    elif isinstance(superseded_raw, list):
        superseded_records = [
            item for item in superseded_raw if isinstance(item, dict)
        ]
    elif superseded_raw is None:
        superseded_records = []
    else:
        raise BootstrapInputError(
            "academic_correction_chain_mismatch",
            "Superseded ledger history must be an object or list.",
        )
    superseded_ledgers = {
        str(item.get("ledger_sha256") or "") for item in superseded_records
    }

    output: dict[str, dict[str, Any]] = {}
    for correction in corrections:
        doi = _normalize_doi(str(correction.get("doi") or ""))
        issue = issue_by_doi.get(doi)
        authoritative_value = correction.get("authoritative_value")
        authoritative_ledger = correction.get("authoritative_ledger")
        superseded_ledger = correction.get("superseded_ledger")
        issue_year_values = {
            value
            for key, value in (issue or {}).items()
            if key.endswith("_year")
            and key
            not in {"superseded_staged_year", "authoritative_staged_year"}
            and isinstance(value, int)
        }
        exact_hash = str((issue or {}).get("exact_content_sha256") or "")
        if (
            not doi
            or doi in output
            or correction.get("field") != "published_year"
            or not isinstance(authoritative_value, int)
            or correction.get("exact_full_text_changed") is not False
            or authoritative_ledger not in {None, ledger_sha256}
            or issue is None
            or issue.get("status") != "resolved_in_authoritative_ledger"
            or issue.get("disposition")
            != "resolved_eligible_for_source_review"
            or issue.get("authoritative_staged_year") != authoritative_value
            or authoritative_value not in issue_year_values
            or issue.get("exact_content_changed") is not False
            or len(exact_hash) != 64
            or any(value not in "0123456789abcdef" for value in exact_hash)
            or (
                correction.get("superseded_value") is not None
                and correction.get("superseded_value")
                != issue.get(
                    "superseded_staged_year",
                    issue.get("superseded_value"),
                )
            )
            or (
                superseded_ledger is not None
                and superseded_ledger not in superseded_ledgers
            )
        ):
            raise BootstrapInputError(
                "academic_correction_chain_mismatch",
                "Independent audit correction is not bound to its DOI, content, and authoritative metadata.",
            )
        output[doi] = {**correction, "doi": doi, "issue": issue}

    if set(output) != set(issue_by_doi):
        raise BootstrapInputError(
            "academic_correction_chain_mismatch",
            "Every resolved metadata issue must have one matching correction.",
        )
    return output


def _validate_source_payload(
    entry: dict[str, Any],
    wrapper: dict[str, Any],
    command: SourceVersionCommand,
    audit: dict[str, Any],
    *,
    lane: str,
) -> None:
    content_hash = _sha256_bytes(command.content.encode("utf-8"))
    if (
        wrapper.get("schema_version") != "generic-source-submission-dry-run-0.1"
        or wrapper.get("status") != "dry_run_not_submitted"
        or wrapper.get("content_sha256") != content_hash
        or (entry.get("exact_content") or {}).get("content_sha256") != content_hash
    ):
        raise BootstrapInputError(
            "exact_content_commitment_mismatch",
            "Source payload does not match its staged exact-content commitment.",
        )
    audited_content_hash = str(audit.get("content_sha256") or "")
    if audited_content_hash and audited_content_hash != content_hash:
        raise BootstrapInputError(
            "audit_content_hash_mismatch",
            "Strict semantic audit content hash does not match Source payload.",
        )
    common_unsafe = (
        command.content_access not in _EXACT_CONTENT_CHANNELS
        or command.requested_extraction_permission != ExtractionPermission.NONE
        or command.requested_storage_permission != StoragePermission.NONE
        or command.requested_scope_locators
        or command.citation_verification
        or command.access.redistribution_allowed
    )
    if lane == "practitioner":
        lane_unsafe = (
            command.source_category != SourceCategory.PRACTITIONER
            or command.knowledge_origin != KnowledgeOrigin.EXPERT_PRACTICE
            or command.citation.peer_review_status
            != PeerReviewStatus.NOT_APPLICABLE
            or command.sensitive_information_level
            != SensitiveInformationLevel.CONTROLLED
        )
    elif lane == "academic":
        lane_unsafe = (
            command.source_category != SourceCategory.ACADEMIC
            or command.source_type.value != "journal_article"
            or command.knowledge_origin != KnowledgeOrigin.SCIENTIFIC_EVIDENCE
            or command.citation.peer_review_status
            != PeerReviewStatus.PEER_REVIEWED
            or command.sensitive_information_level
            != SensitiveInformationLevel.PUBLIC
        )
    else:  # Defensive: callers already validate the lane before reading inputs.
        raise BootstrapInputError("unsupported_lane", "Unsupported Bootstrap lane.")
    if common_unsafe or lane_unsafe:
        raise BootstrapInputError(
            "unsafe_source_payload",
            f"Source payload violates {lane} Bootstrap safety constraints.",
        )
    if _canonical_url(command.citation.url) != _canonical_url(str(entry["canonical_url"])):
        raise BootstrapInputError(
            "source_locator_mismatch", "Citation and ledger locator do not match."
        )
    if (
        command.source_category.value != str(entry.get("source_category") or "")
        or command.source_type.value != str(entry.get("source_type") or "")
        or command.knowledge_origin.value != str(entry.get("knowledge_origin") or "")
        or command.sensitive_information_level.value
        != str(entry.get("sensitive_information_level") or "")
        or command.citation.year != entry.get("published_year")
        or command.citation.authors != [str(value) for value in entry.get("authors", [])]
        or str(command.citation.title).strip()
        != str(entry.get("title") or "").strip()
    ):
        raise BootstrapInputError(
            "ledger_payload_metadata_mismatch",
            "Source payload metadata does not match its immutable ledger entry.",
        )
    if str(command.citation.title).strip() != str(audit.get("title") or "").strip():
        raise BootstrapInputError(
            "audit_title_mismatch", "Audit title does not match Source payload."
        )
    audit_authors = audit.get("authors")
    if audit_authors is not None and command.citation.authors != [
        str(value) for value in audit_authors
    ]:
        raise BootstrapInputError(
            "audit_author_mismatch", "Audit author metadata does not match Source payload."
        )
    if lane == "academic":
        command_doi = _normalize_doi(command.citation.doi or "")
        if (
            not command_doi
            or command_doi != _normalize_doi(str(entry.get("doi") or ""))
            or command_doi != _normalize_doi(str(audit.get("doi") or ""))
        ):
            raise BootstrapInputError(
                "academic_doi_mismatch",
                "Academic DOI does not match payload, ledger, and audit.",
            )
        audited_year = audit.get("published_year")
        if audited_year is not None and command.citation.year != audited_year:
            raise BootstrapInputError(
                "academic_year_mismatch",
                "Academic publication year does not match the corrected audit.",
            )
        if (
            len(command.content) != audit.get("content_characters")
            or len(command.content.encode("utf-8")) != audit.get("content_bytes")
            or len(command.content.encode("utf-8"))
            != (entry.get("exact_content") or {}).get("content_bytes")
        ):
            raise BootstrapInputError(
                "academic_content_length_mismatch",
                "Academic content length does not match ledger and both audits.",
            )


def _safe_payload_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    allowed = (root / "source_payloads").resolve()
    if allowed not in candidate.parents:
        raise BootstrapInputError(
            "unsafe_payload_path", "Ledger payload path escapes source_payloads."
        )
    return candidate


def _canonical_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    host = parts.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_KEYS
        )
    )
    return urlunsplit(
        (parts.scheme.casefold(), host, parts.path.rstrip("/") or "/", query, "")
    )


def _normalize_doi(value: str) -> str:
    normalized = value.strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            return normalized[len(prefix) :]
    return normalized


def _normalize_title_binding(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).translate(
        str.maketrans({character: "-" for character in "‐‑‒–—−"})
    )
    return " ".join(normalized.casefold().split())


def _read_object(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapInputError(code, f"Could not read {path}.") from exc
    if not isinstance(value, dict):
        raise BootstrapInputError(code, f"Expected a JSON object in {path}.")
    return value


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _write_immutable(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise BootstrapInputError(
            "output_path_not_isolated",
            f"Immutable output cannot be written through a symbolic link: {path}.",
        )
    if path.exists():
        if path.read_bytes() != data:
            raise BootstrapInputError(
                "immutable_output_conflict", f"Existing output differs: {path}."
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
        os.chmod(path, 0o600)
    except FileExistsError:
        if path.read_bytes() != data:
            raise BootstrapInputError(
                "immutable_output_conflict", f"Existing output differs: {path}."
            )


if __name__ == "__main__":
    raise SystemExit(main())
