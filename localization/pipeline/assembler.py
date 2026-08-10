"""Verified assembly of completed localization proposal runs.

Assembly is deliberately a local artifact operation.  It never calls a model,
edits the runtime catalog, writes Translation Memory, or promotes a proposal.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from localization.pipeline.artifacts import ArtifactError, read_jsonl
from localization.pipeline.models import SourceUnit, TranslationProposal


@dataclass(frozen=True)
class VerifiedProposalRun:
    """One completed, integrity-checked GLM proposal run."""

    path: Path
    run_id: str
    manifest_sha256: str
    proposals_sha256: str
    proposal_count: int
    llm_provider: str
    llm_model: str
    canonical_catalog_sha256: str
    proposals: tuple[TranslationProposal, ...]

    def lineage_record(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "path": str(self.path),
            "manifest_sha256": self.manifest_sha256,
            "proposals_sha256": self.proposals_sha256,
            "proposal_count": self.proposal_count,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "canonical_catalog_sha256": self.canonical_catalog_sha256,
        }


@dataclass(frozen=True)
class AssemblyResult:
    """Deterministic overlay result in canonical catalog order."""

    source_units: tuple[SourceUnit, ...]
    proposals: tuple[TranslationProposal, ...]
    upstream_runs: tuple[VerifiedProposalRun, ...]
    overridden_keys: tuple[str, ...]
    override_events: tuple[dict[str, str], ...]


def assemble_verified_runs(
    input_runs: Iterable[Path],
    current_units: Iterable[SourceUnit],
    *,
    canonical_catalog_sha256: str,
    require_complete_catalog: bool = False,
) -> AssemblyResult:
    """Load and overlay trusted proposal runs, with later inputs winning.

    Proposal wording and machine provenance are never edited.  Catalog identity
    fields are reattached from the current local ``SourceUnit`` only after the
    upstream record has proved that its key, source text, and source hash still
    match that unit.
    """

    paths = [Path(path).resolve() for path in input_runs]
    if not paths:
        raise ArtifactError("assemble requires at least one --input-run")
    if len(paths) != len(set(paths)):
        raise ArtifactError("assemble input runs must be unique paths")

    units = tuple(current_units)
    units_by_key = {unit.key: unit for unit in units}
    if len(units_by_key) != len(units):
        raise ArtifactError("current catalog contains duplicate source-unit keys")

    verified_runs = tuple(
        _load_verified_run(
            path,
            units_by_key,
            canonical_catalog_sha256=canonical_catalog_sha256,
        )
        for path in paths
    )

    selected: dict[str, TranslationProposal] = {}
    selected_run: dict[str, str] = {}
    override_events: list[dict[str, str]] = []
    for run in verified_runs:
        for proposal in run.proposals:
            if proposal.key in selected:
                override_events.append(
                    {
                        "key": proposal.key,
                        "replaced_run_id": selected_run[proposal.key],
                        "replacement_run_id": run.run_id,
                    }
                )
            unit = units_by_key[proposal.key]
            selected[proposal.key] = _reattach_trusted_source_metadata(proposal, unit)
            selected_run[proposal.key] = run.run_id

    selected_keys = set(selected)
    catalog_keys = set(units_by_key)
    if require_complete_catalog and selected_keys != catalog_keys:
        missing = sorted(catalog_keys - selected_keys)
        extra = sorted(selected_keys - catalog_keys)
        details = []
        if missing:
            details.append(f"missing {len(missing)} key(s): {', '.join(missing[:12])}")
        if extra:
            details.append(f"extra {len(extra)} key(s): {', '.join(extra[:12])}")
        raise ArtifactError(
            "assembled proposal set does not equal the current catalog; "
            + "; ".join(details)
        )

    ordered_units = tuple(unit for unit in units if unit.key in selected)
    ordered_proposals = tuple(selected[unit.key] for unit in ordered_units)
    return AssemblyResult(
        source_units=ordered_units,
        proposals=ordered_proposals,
        upstream_runs=verified_runs,
        overridden_keys=tuple(sorted({event["key"] for event in override_events})),
        override_events=tuple(override_events),
    )


def _load_verified_run(
    run_path: Path,
    units_by_key: dict[str, SourceUnit],
    *,
    canonical_catalog_sha256: str,
) -> VerifiedProposalRun:
    if not run_path.is_dir():
        raise ArtifactError(f"input run directory not found: {run_path}")
    manifest_path = run_path / "manifest.json"
    proposals_path = run_path / "proposals.jsonl"
    if not manifest_path.is_file():
        raise ArtifactError(f"input run has no manifest.json: {run_path}")
    if not proposals_path.is_file():
        raise ArtifactError(f"input run has no proposals.jsonl: {run_path}")

    manifest_payload = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_payload)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"invalid input manifest JSON at {manifest_path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ArtifactError(f"input manifest must be an object: {manifest_path}")

    run_id = _required_string(manifest, "run_id", manifest_path)
    if run_id != run_path.name:
        raise ArtifactError(
            f"input manifest run_id {run_id!r} does not match directory {run_path.name!r}"
        )
    if manifest.get("status") != "completed":
        raise ArtifactError(f"input run {run_id} is not completed")
    if manifest.get("command") != "propose":
        raise ArtifactError(f"input run {run_id} is not a propose run")
    if manifest.get("validation_passed") is not True:
        raise ArtifactError(f"input run {run_id} did not pass deterministic validation")
    if manifest.get("llm_invoked") is not True:
        raise ArtifactError(f"input run {run_id} has no verified GLM generation lineage")
    if str(manifest.get("llm_provider", "")).casefold() != "glm":
        raise ArtifactError(f"input run {run_id} was not generated by GLM")
    if manifest.get("external_translation_platform_used") is not False:
        raise ArtifactError(
            f"input run {run_id} does not prove external translation platforms were unused"
        )
    if manifest.get("human_translator_used") is not False:
        raise ArtifactError(
            f"input run {run_id} does not prove human translators were unused"
        )
    if manifest.get("runtime_modified") is not False:
        raise ArtifactError(f"input run {run_id} does not prove runtime remained unchanged")
    if manifest.get("translation_memory_modified") is not False:
        raise ArtifactError(
            f"input run {run_id} does not prove Translation Memory remained unchanged"
        )
    if manifest.get("canonical_catalog_sha256") != canonical_catalog_sha256:
        raise ArtifactError(f"input run {run_id} targets a different canonical catalog")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ArtifactError(f"input run {run_id} manifest has no artifact fingerprints")
    recorded = artifacts.get("proposals.jsonl")
    if not isinstance(recorded, dict):
        raise ArtifactError(f"input run {run_id} does not fingerprint proposals.jsonl")

    proposals_payload = proposals_path.read_bytes()
    proposals_sha256 = hashlib.sha256(proposals_payload).hexdigest()
    if recorded.get("sha256") != proposals_sha256:
        raise ArtifactError(f"input run {run_id} proposals.jsonl SHA-256 mismatch")
    recorded_bytes = recorded.get("bytes")
    if recorded_bytes is not None and recorded_bytes != len(proposals_payload):
        raise ArtifactError(f"input run {run_id} proposals.jsonl byte count mismatch")

    raw_proposals = read_jsonl(proposals_path)
    if not raw_proposals:
        raise ArtifactError(f"input run {run_id} contains no proposals")
    try:
        proposals = tuple(TranslationProposal.model_validate(item) for item in raw_proposals)
    except ValidationError as exc:
        raise ArtifactError(f"input run {run_id} has an invalid proposal schema: {exc}") from exc

    proposal_count = manifest.get("proposal_count")
    unit_count = manifest.get("unit_count")
    if proposal_count != len(proposals) or unit_count != len(proposals):
        raise ArtifactError(
            f"input run {run_id} proposal/unit counts do not match proposals.jsonl"
        )
    proposal_keys = [proposal.key for proposal in proposals]
    if len(proposal_keys) != len(set(proposal_keys)):
        raise ArtifactError(f"input run {run_id} contains duplicate proposal keys")

    for proposal in proposals:
        unit = units_by_key.get(proposal.key)
        if unit is None:
            raise ArtifactError(
                f"input run {run_id} proposal key is absent from the current catalog: {proposal.key}"
            )
        if proposal.source_text != unit.source_text:
            raise ArtifactError(
                f"input run {run_id} has stale source text for key {proposal.key}"
            )
        if proposal.source_hash != unit.source_hash:
            raise ArtifactError(
                f"input run {run_id} has stale source hash for key {proposal.key}"
            )

    return VerifiedProposalRun(
        path=run_path,
        run_id=run_id,
        manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
        proposals_sha256=proposals_sha256,
        proposal_count=len(proposals),
        llm_provider=_required_string(manifest, "llm_provider", manifest_path),
        llm_model=_required_string(manifest, "llm_model", manifest_path),
        canonical_catalog_sha256=canonical_catalog_sha256,
        proposals=proposals,
    )


def _reattach_trusted_source_metadata(
    proposal: TranslationProposal,
    unit: SourceUnit,
) -> TranslationProposal:
    """Rebind only fields whose authority is the current canonical catalog."""

    return proposal.model_copy(
        update={
            "source_text": unit.source_text,
            "source_hash": unit.source_hash,
            "protected_terms": unit.protected_terms,
            "source_locale": unit.source_locale,
            "target_locale": unit.target_locale,
        }
    )


def _required_string(manifest: dict[str, Any], key: str, path: Path) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ArtifactError(f"input manifest {path} requires non-empty string {key!r}")
    return value
