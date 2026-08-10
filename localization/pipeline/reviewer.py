"""GLM-only semantic quality review for assembled localization candidates.

The reviewer is intentionally non-authoritative. It verifies a completed
assembly run, sends only explicitly selected UI records to the existing GLM
adapter, and returns keep/revise/needs-human-review recommendations. It never
writes runtime messages, Translation Memory, or approval state.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from localization.pipeline.artifacts import ArtifactError, read_jsonl
from localization.pipeline.models import (
    GenerationOrigin,
    LocalizationIssueType,
    ProposalStatus,
    QualityReviewDecision,
    QualityReviewDisposition,
    QualityReviewNormalization,
    SourceUnit,
    TranslationProposal,
    ValidationReport,
)
from localization.pipeline.validator import LocalizationValidator


PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "ui-quality-reviewer-v1.md"
DEFAULT_BATCH_SIZE = 8
MAX_BATCH_SIZE = 16
DEFAULT_BATCH_CHARACTER_BUDGET = 14_000


class LocalizationQualityReviewError(RuntimeError):
    """Raised when review input or model output violates the review contract."""


class StructuredGLMClient(Protocol):
    def generate_structured(
        self,
        prompt: str,
        response_model: type[BaseModel],
        **kwargs: Any,
    ) -> Any: ...


class _GLMReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    disposition: QualityReviewDisposition
    issue_types: list[LocalizationIssueType] = Field(default_factory=list)
    # Raw model explanations may be verbose. The trusted conversion layer
    # deterministically normalizes these into the <=500-character durable
    # QualityReviewDecision contract.
    rationale: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0.0, le=1.0)
    revised_candidate_chinese: str | None = None

    @model_validator(mode="after")
    def validate_disposition_contract(self) -> "_GLMReviewItem":
        if self.disposition == QualityReviewDisposition.KEEP:
            if self.issue_types:
                raise ValueError("keep decisions cannot declare issue_types")
            if self.revised_candidate_chinese is not None:
                raise ValueError("keep decisions cannot contain revised text")
        elif self.disposition == QualityReviewDisposition.REVISE:
            if not self.issue_types:
                raise ValueError("revise decisions require at least one issue_type")
            # A missing revision is a known model contradiction. It is allowed
            # through schema parsing only so the trusted conversion layer can
            # downgrade it to needs_human_review with explicit provenance.
        else:
            if not self.issue_types:
                raise ValueError("needs_human_review requires at least one issue_type")
            if self.revised_candidate_chinese is not None:
                raise ValueError("needs_human_review cannot contain revised text")
        return self


class _GLMReviewBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[_GLMReviewItem] = Field(min_length=1, max_length=MAX_BATCH_SIZE)


@dataclass(frozen=True)
class VerifiedAssemblyRun:
    """A completed, integrity-checked candidate run used as review input."""

    path: Path
    run_id: str
    command: Literal["assemble", "quality-review"]
    proposals_artifact: Literal["proposals.jsonl", "final-proposals.jsonl"]
    manifest_sha256: str
    proposals_sha256: str
    proposal_count: int
    canonical_catalog_sha256: str
    upstream_runs: tuple[dict[str, Any], ...]
    proposals: tuple[TranslationProposal, ...]

    def lineage_record(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "path": str(self.path),
            "command": self.command,
            "proposals_artifact": self.proposals_artifact,
            "manifest_sha256": self.manifest_sha256,
            "proposals_sha256": self.proposals_sha256,
            "proposal_count": self.proposal_count,
            "canonical_catalog_sha256": self.canonical_catalog_sha256,
            "upstream_runs": list(self.upstream_runs),
        }


@dataclass(frozen=True)
class QualityReviewResult:
    """Ordered second-pass review output for the selected catalog units."""

    decisions: tuple[QualityReviewDecision, ...]
    proposals: tuple[TranslationProposal, ...]


def load_verified_assembly_run(
    input_run: Path,
    current_units: Iterable[SourceUnit],
    *,
    canonical_catalog_sha256: str,
) -> VerifiedAssemblyRun:
    """Verify an assembly or completed quality-review candidate package."""

    run_path = Path(input_run).resolve()
    if not run_path.is_dir():
        raise ArtifactError(f"quality-review input run directory not found: {run_path}")
    manifest_path = run_path / "manifest.json"
    if not manifest_path.is_file():
        raise ArtifactError(f"quality-review input has no manifest.json: {run_path}")

    manifest_payload = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_payload)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"invalid quality-review input manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise ArtifactError("quality-review input manifest must be an object")

    run_id = _required_string(manifest, "run_id")
    if run_id != run_path.name:
        raise ArtifactError("quality-review input run_id does not match its directory")
    command = manifest.get("command")
    if command not in {"assemble", "quality-review"}:
        raise ArtifactError(
            f"quality-review input {run_id} must be an assemble or quality-review run"
        )
    required_values = {
        "status": "completed",
        "validation_passed": True,
        "external_translation_platform_used": False,
        "human_translator_used": False,
        "runtime_modified": False,
        "translation_memory_modified": False,
    }
    for field, expected in required_values.items():
        if manifest.get(field) != expected:
            raise ArtifactError(
                f"quality-review input {run_id} requires {field}={expected!r}"
            )
    if manifest.get("canonical_catalog_sha256") != canonical_catalog_sha256:
        raise ArtifactError(
            f"quality-review input {run_id} targets a different canonical catalog"
        )

    selected_review_results: dict[str, TranslationProposal] = {}
    if command == "assemble":
        if manifest.get("upstream_llm_invoked") is not True:
            raise ArtifactError(
                f"quality-review input {run_id} has no GLM upstream lineage"
            )
        if manifest.get("proposal_provenance") != "assembled_verified_glm_propose_runs":
            raise ArtifactError(
                f"quality-review input {run_id} lacks verified assembly provenance"
            )
        normalized_upstream = _validate_glm_upstream_records(
            manifest.get("upstream_runs"),
            run_id,
        )
        proposals_artifact = "proposals.jsonl"
        expected_proposal_count = manifest.get("proposal_count")
        if manifest.get("unit_count") != expected_proposal_count:
            raise ArtifactError(
                f"quality-review input {run_id} unit/proposal counts do not agree"
            )
    else:
        if manifest.get("llm_invoked") is not True:
            raise ArtifactError(
                f"quality-review input {run_id} has no GLM review lineage"
            )
        if str(manifest.get("llm_provider", "")).casefold() != "glm":
            raise ArtifactError(
                f"quality-review input {run_id} was not reviewed by GLM"
            )
        if manifest.get("proposal_provenance") not in {
            "second_pass_over_verified_glm_assembly",
            "quality_review_over_verified_candidate_run",
        }:
            raise ArtifactError(
                f"quality-review input {run_id} lacks verified review provenance"
            )
        if manifest.get("full_catalog_overlay") is not True:
            raise ArtifactError(
                f"quality-review input {run_id} is not a full catalog overlay"
            )
        if manifest.get("final_validation_passed") is not True:
            raise ArtifactError(
                f"quality-review input {run_id} did not pass final validation"
            )
        candidate_lineage = manifest.get("input_candidate_run") or manifest.get(
            "input_assembly"
        )
        if not isinstance(candidate_lineage, dict):
            raise ArtifactError(
                f"quality-review input {run_id} has no verified candidate-run lineage"
            )
        for hash_field in ("manifest_sha256", "proposals_sha256"):
            value = candidate_lineage.get(hash_field)
            if not isinstance(value, str) or not _is_sha256(value):
                raise ArtifactError(
                    f"quality-review input {run_id} has invalid candidate {hash_field}"
                )
        if candidate_lineage.get("canonical_catalog_sha256") != canonical_catalog_sha256:
            raise ArtifactError(
                f"quality-review input {run_id} candidate lineage targets another catalog"
            )
        normalized_upstream = _validate_glm_upstream_records(
            candidate_lineage.get("upstream_runs"),
            run_id,
        )
        proposals_artifact = "final-proposals.jsonl"
        expected_proposal_count = manifest.get("final_proposal_count")
        if not isinstance(expected_proposal_count, int) or expected_proposal_count < 1:
            raise ArtifactError(
                f"quality-review input {run_id} has invalid final_proposal_count"
            )

        selected_review_results = _verify_quality_review_decisions(
            run_path,
            manifest,
            run_id,
        )

        validation_payload, _ = _verify_recorded_artifact(
            run_path,
            manifest,
            run_id,
            "final-validation-report.json",
        )
        try:
            final_validation = ValidationReport.model_validate_json(validation_payload)
        except ValidationError as exc:
            raise ArtifactError(
                f"quality-review input {run_id} has an invalid final validation report"
            ) from exc
        if (
            not final_validation.valid
            or final_validation.error_count != 0
            or final_validation.checked_units != expected_proposal_count
            or final_validation.checked_proposals != expected_proposal_count
        ):
            raise ArtifactError(
                f"quality-review input {run_id} final validation report is not PASS"
            )

    proposals_payload, proposals_sha256 = _verify_recorded_artifact(
        run_path,
        manifest,
        run_id,
        proposals_artifact,
    )
    proposals_path = run_path / proposals_artifact

    raw_proposals = read_jsonl(proposals_path)
    if not raw_proposals:
        raise ArtifactError(f"quality-review input {run_id} contains no proposals")
    try:
        proposals = tuple(TranslationProposal.model_validate(item) for item in raw_proposals)
    except ValidationError as exc:
        raise ArtifactError(
            f"quality-review input {run_id} has an invalid proposal schema"
        ) from exc
    if expected_proposal_count != len(proposals):
        raise ArtifactError(
            f"quality-review input {run_id} proposal count does not match {proposals_artifact}"
        )
    keys = [proposal.key for proposal in proposals]
    if len(keys) != len(set(keys)):
        raise ArtifactError(f"quality-review input {run_id} contains duplicate keys")

    units_by_key = {unit.key: unit for unit in current_units}
    if command == "quality-review" and set(keys) != set(units_by_key):
        raise ArtifactError(
            f"quality-review input {run_id} final proposals do not cover the current catalog"
        )
    if command == "quality-review":
        final_by_key = {proposal.key: proposal for proposal in proposals}
        for key, selected_result in selected_review_results.items():
            final = final_by_key.get(key)
            if final != selected_result:
                raise ArtifactError(
                    f"quality-review input {run_id} final overlay does not contain "
                    f"the selected result for {key}"
                )
    for proposal in proposals:
        unit = units_by_key.get(proposal.key)
        if unit is None:
            raise ArtifactError(
                f"quality-review input key is absent from current catalog: {proposal.key}"
            )
        if proposal.source_text != unit.source_text or proposal.source_hash != unit.source_hash:
            raise ArtifactError(
                f"quality-review input has stale source identity for key {proposal.key}"
            )
        if (
            proposal.provider != "glm"
            or proposal.generated_by != GenerationOrigin.LOCAL_AI
            or proposal.status != ProposalStatus.MACHINE_PROPOSED
        ):
            raise ArtifactError(
                f"quality-review input proposal {proposal.key} is not a GLM machine proposal"
            )

    return VerifiedAssemblyRun(
        path=run_path,
        run_id=run_id,
        command=command,
        proposals_artifact=proposals_artifact,
        manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
        proposals_sha256=proposals_sha256,
        proposal_count=len(proposals),
        canonical_catalog_sha256=canonical_catalog_sha256,
        upstream_runs=tuple(normalized_upstream),
        proposals=proposals,
    )


def select_review_candidates(
    verified_run: VerifiedAssemblyRun,
    current_units: Iterable[SourceUnit],
    *,
    keys: Iterable[str] = (),
    changed_only: bool = False,
) -> tuple[tuple[SourceUnit, ...], tuple[TranslationProposal, ...]]:
    """Select review candidates in assembly order, with strict key checking."""

    units_by_key = {unit.key: unit for unit in current_units}
    proposals_by_key = {proposal.key: proposal for proposal in verified_run.proposals}
    requested = [key.strip() for key in keys if key.strip()]
    unknown = sorted(set(requested) - set(proposals_by_key))
    if unknown:
        raise ArtifactError(
            "quality-review keys are absent from the verified assembly: "
            + ", ".join(unknown[:20])
        )
    selected_keys = set(requested)
    if changed_only:
        selected_keys.update(
            proposal.key
            for proposal in verified_run.proposals
            if proposal.candidate_chinese != units_by_key[proposal.key].current_target
        )
    if not selected_keys:
        raise ArtifactError(
            "quality-review requires --key, --keys-file, or --changed-only"
        )

    proposals = tuple(
        proposal for proposal in verified_run.proposals if proposal.key in selected_keys
    )
    units = tuple(units_by_key[proposal.key] for proposal in proposals)
    return units, proposals


def read_review_keys(path: Path) -> tuple[str, ...]:
    """Read a newline-delimited key list; blank lines and comments are ignored."""

    key_path = Path(path).resolve()
    if not key_path.is_file():
        raise ArtifactError(f"quality-review keys file not found: {key_path}")
    keys: list[str] = []
    for line_number, raw in enumerate(key_path.read_text(encoding="utf-8").splitlines(), 1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if any(character.isspace() for character in value):
            raise ArtifactError(
                f"quality-review keys file has invalid whitespace at line {line_number}"
            )
        keys.append(value)
    return tuple(dict.fromkeys(keys))


class GLMLocalizationQualityReviewer:
    """Run a bounded second-pass semantic review through the existing GLM client."""

    def __init__(
        self,
        llm: StructuredGLMClient,
        *,
        validator: LocalizationValidator,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_character_budget: int = DEFAULT_BATCH_CHARACTER_BUDGET,
        prompt_path: Path = PROMPT_PATH,
    ) -> None:
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
        if batch_character_budget < 1_000:
            raise ValueError("batch_character_budget must be at least 1000")
        self.llm = llm
        self.validator = validator
        self.batch_size = batch_size
        self.batch_character_budget = batch_character_budget
        try:
            self.system_prompt = prompt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LocalizationQualityReviewError(
                f"could not load localization quality-review prompt: {prompt_path}"
            ) from exc

    def review(
        self,
        units: Iterable[SourceUnit],
        proposals: Iterable[TranslationProposal],
    ) -> QualityReviewResult:
        source_units = tuple(units)
        input_proposals = tuple(proposals)
        self._validate_inputs(source_units, input_proposals)
        decisions: list[QualityReviewDecision] = []
        result_proposals: list[TranslationProposal] = []
        for batch_units, batch_proposals in self._partition(source_units, input_proposals):
            response = self._generate_batch(batch_units, batch_proposals)
            batch_decisions, batch_results = self._convert_batch(
                batch_units,
                batch_proposals,
                response,
            )
            decisions.extend(batch_decisions)
            result_proposals.extend(batch_results)

        report = self.validator.validate_batch(source_units, result_proposals)
        if not report.valid:
            issue = next(item for item in report.issues if item.severity.value == "error")
            raise LocalizationQualityReviewError(
                f"quality-review output failed deterministic validation for {issue.key!r}: "
                f"{issue.code}"
            )
        return QualityReviewResult(tuple(decisions), tuple(result_proposals))

    def _validate_inputs(
        self,
        units: tuple[SourceUnit, ...],
        proposals: tuple[TranslationProposal, ...],
    ) -> None:
        if not units or not proposals:
            raise LocalizationQualityReviewError("quality review requires candidates")
        report = self.validator.validate_batch(units, proposals)
        if not report.valid:
            issue = next(item for item in report.issues if item.severity.value == "error")
            raise LocalizationQualityReviewError(
                f"quality-review input failed deterministic validation for {issue.key!r}: "
                f"{issue.code}"
            )

    def _partition(
        self,
        units: tuple[SourceUnit, ...],
        proposals: tuple[TranslationProposal, ...],
    ) -> list[tuple[list[SourceUnit], list[TranslationProposal]]]:
        batches: list[tuple[list[SourceUnit], list[TranslationProposal]]] = []
        current_units: list[SourceUnit] = []
        current_proposals: list[TranslationProposal] = []
        current_characters = 0
        for unit, proposal in zip(units, proposals, strict=True):
            size = len(json.dumps(self._unit_payload(unit, proposal), ensure_ascii=False))
            if size > self.batch_character_budget:
                raise LocalizationQualityReviewError(
                    f"UI review unit {unit.key!r} exceeds the character budget"
                )
            if current_units and (
                len(current_units) >= self.batch_size
                or current_characters + size > self.batch_character_budget
            ):
                batches.append((current_units, current_proposals))
                current_units, current_proposals, current_characters = [], [], 0
            current_units.append(unit)
            current_proposals.append(proposal)
            current_characters += size
        if current_units:
            batches.append((current_units, current_proposals))
        return batches

    def _generate_batch(
        self,
        units: list[SourceUnit],
        proposals: list[TranslationProposal],
    ) -> _GLMReviewBatch:
        payload = {
            "source_locale": "en-US",
            "target_locale": "zh-CN",
            "units": [
                self._unit_payload(unit, proposal)
                for unit, proposal in zip(units, proposals, strict=True)
            ],
        }
        prompt = (
            "Review this bounded batch of machine-proposed zh-CN UI candidates.\n"
            "Return only the supplied structured schema.\n"
            f"{json.dumps(_GLMReviewBatch.model_json_schema(), ensure_ascii=False)}\n\n"
            "REVIEW_INPUT:\n"
            f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
        )
        response = self.llm.generate_structured(
            prompt,
            _GLMReviewBatch,
            system_prompt=self.system_prompt,
            temperature=0.0,
            max_tokens=4_000,
            thinking_enabled=False,
        )
        if isinstance(response, _GLMReviewBatch):
            return response
        try:
            return _GLMReviewBatch.model_validate(response)
        except Exception as exc:
            raise LocalizationQualityReviewError(
                "GLM returned a response outside the quality-review schema"
            ) from exc

    def _unit_payload(
        self,
        unit: SourceUnit,
        proposal: TranslationProposal,
    ) -> dict[str, Any]:
        return {
            "key": unit.key,
            "source_text": unit.source_text,
            "localization_source": self.validator.canonicalize_source_terms(
                unit.source_text
            ),
            "current_runtime_target": unit.current_target,
            "machine_candidate": proposal.candidate_chinese,
            "classification": unit.classification.value,
            "action": unit.action.value,
            "placeholders": list(unit.placeholders),
            "protected_terms": list(unit.protected_terms),
            "required_target_terms": [
                {
                    "canonical": term,
                    "minimum_occurrences": self.validator.source_term_occurrence_count(
                        unit.source_text,
                        term,
                    ),
                }
                for term in unit.protected_terms
            ],
            "context": list(unit.context),
        }

    def _convert_batch(
        self,
        units: list[SourceUnit],
        proposals: list[TranslationProposal],
        response: _GLMReviewBatch,
    ) -> tuple[list[QualityReviewDecision], list[TranslationProposal]]:
        expected_keys = [unit.key for unit in units]
        actual_keys = [decision.key for decision in response.decisions]
        if actual_keys != expected_keys:
            raise LocalizationQualityReviewError(
                "GLM review keys must match input keys exactly and in input order"
            )

        decisions: list[QualityReviewDecision] = []
        results: list[TranslationProposal] = []
        for unit, original, generated in zip(
            units, proposals, response.decisions, strict=True
        ):
            revised: TranslationProposal | None = None
            result = original
            disposition = generated.disposition
            normalization: QualityReviewNormalization | None = None
            normalization_issue_codes: tuple[str, ...] = ()
            issue_types = tuple(generated.issue_types)
            rationale = _bounded_review_rationale(generated.rationale)
            if generated.disposition == QualityReviewDisposition.REVISE:
                if (
                    generated.revised_candidate_chinese is None
                    or not generated.revised_candidate_chinese.strip()
                ):
                    disposition = QualityReviewDisposition.NEEDS_HUMAN_REVIEW
                    normalization = (
                        QualityReviewNormalization.MISSING_REVISION_CANDIDATE
                    )
                    rationale = _missing_revision_rationale(generated.rationale)
                elif generated.revised_candidate_chinese == original.candidate_chinese:
                    # A contradictory revise response is not accepted as a
                    # revision and must not abort an otherwise valid batch.
                    # Preserve the candidate and route the conflict to human
                    # review with explicit machine provenance.
                    disposition = QualityReviewDisposition.NEEDS_HUMAN_REVIEW
                    normalization = (
                        QualityReviewNormalization.UNCHANGED_REVISION_CONFLICT
                    )
                    rationale = _unchanged_revision_rationale(generated.rationale)
                else:
                    revised = TranslationProposal(
                        key=unit.key,
                        source_text=unit.source_text,
                        source_hash=unit.source_hash,
                        candidate_chinese=generated.revised_candidate_chinese or "",
                        rationale=rationale,
                        confidence=generated.confidence,
                        status=ProposalStatus.MACHINE_PROPOSED,
                        provider="glm",
                        warnings=original.warnings,
                        protected_terms=unit.protected_terms,
                        generated_by=GenerationOrigin.LOCAL_AI,
                        source_locale=unit.source_locale,
                        target_locale=unit.target_locale,
                    )
                    revision_report = self.validator.validate_batch([unit], [revised])
                    if not revision_report.valid:
                        normalization_issue_codes = tuple(
                            dict.fromkeys(
                                issue.code
                                for issue in revision_report.issues
                                if issue.severity.value == "error"
                            )
                        )
                        disposition = QualityReviewDisposition.NEEDS_HUMAN_REVIEW
                        normalization = (
                            QualityReviewNormalization.INVALID_REVISION_POLICY
                        )
                        issue_types = tuple(
                            dict.fromkeys(
                                (
                                    *issue_types,
                                    *_validator_risk_issue_types(
                                        normalization_issue_codes
                                    ),
                                )
                            )
                        )
                        rationale = _invalid_revision_policy_rationale(
                            generated.rationale,
                            normalization_issue_codes,
                        )
                        revised = None
                    else:
                        result = revised

            decision = QualityReviewDecision(
                key=unit.key,
                model_disposition=generated.disposition,
                disposition=disposition,
                normalization=normalization,
                normalization_issue_codes=normalization_issue_codes,
                issue_types=issue_types,
                rationale=rationale,
                confidence=generated.confidence,
                input_candidate_chinese=original.candidate_chinese,
                result_candidate_chinese=result.candidate_chinese,
                input_proposal_sha256=proposal_sha256(original),
                revised_proposal=revised,
                reviewer_provider="glm",
            )
            decisions.append(decision)
            results.append(result)
        return decisions, results


def proposal_sha256(proposal: TranslationProposal) -> str:
    payload = json.dumps(
        proposal.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _unchanged_revision_rationale(model_rationale: str) -> str:
    """Append a bounded, explicit trace for a contradictory revise response."""

    suffix = (
        "Normalization: unchanged revision conflict; GLM requested revise "
        "but returned the input candidate unchanged."
    )
    return _append_normalization_rationale(model_rationale, suffix)


def _missing_revision_rationale(model_rationale: str) -> str:
    suffix = (
        "Normalization: missing revision candidate; GLM requested revise "
        "but returned no usable revised_candidate_chinese."
    )
    return _append_normalization_rationale(model_rationale, suffix)


def _invalid_revision_policy_rationale(
    model_rationale: str,
    issue_codes: tuple[str, ...],
) -> str:
    suffix = (
        "Normalization: invalid revision policy; GLM revision was rejected by "
        "deterministic validator issue(s): "
        + ", ".join(issue_codes)
        + "."
    )
    return _append_normalization_rationale(model_rationale, suffix)


def _validator_risk_issue_types(
    issue_codes: tuple[str, ...],
) -> tuple[LocalizationIssueType, ...]:
    risks: list[LocalizationIssueType] = []
    for code in issue_codes:
        if "placeholder" in code:
            risks.append(LocalizationIssueType.PLACEHOLDER_RISK)
        elif (
            "term" in code
            or "preserve_exact" in code
            or "product_name" in code
            or "bilingual" in code
        ):
            risks.append(LocalizationIssueType.PROTECTED_TERM_RISK)
        else:
            risks.append(LocalizationIssueType.OTHER)
    return tuple(dict.fromkeys(risks))


def _append_normalization_rationale(model_rationale: str, suffix: str) -> str:
    normalized_suffix = " ".join(suffix.split())
    if len(normalized_suffix) > 500:
        normalized_suffix = normalized_suffix[-500:]
    prefix_budget = 500 - len(normalized_suffix) - 1
    normalized_prefix = " ".join(model_rationale.split())
    prefix = normalized_prefix[: max(0, prefix_budget)].rstrip()
    result = f"{prefix} {normalized_suffix}" if prefix else normalized_suffix
    return result[:500]


def _bounded_review_rationale(model_rationale: str) -> str:
    """Normalize verbose raw GLM rationale into the durable 500-char field."""

    normalized = " ".join(model_rationale.split())
    if len(normalized) <= 500:
        return normalized
    suffix = " [truncated]"
    return normalized[: 500 - len(suffix)].rstrip() + suffix


def _required_string(manifest: dict[str, Any], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ArtifactError(f"quality-review input manifest requires non-empty {key!r}")
    return value


def _validate_glm_upstream_records(
    upstream: object,
    run_id: str,
) -> list[dict[str, Any]]:
    if not isinstance(upstream, list) or not upstream:
        raise ArtifactError(f"quality-review input {run_id} has no GLM upstream lineage")
    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(upstream):
        if not isinstance(record, dict):
            raise ArtifactError(
                f"quality-review input {run_id} has invalid upstream lineage record {index}"
            )
        if str(record.get("llm_provider", "")).casefold() != "glm":
            raise ArtifactError(
                f"quality-review input {run_id} contains non-GLM upstream lineage"
            )
        for hash_field in ("manifest_sha256", "proposals_sha256"):
            value = record.get(hash_field)
            if not isinstance(value, str) or not _is_sha256(value):
                raise ArtifactError(
                    f"quality-review input {run_id} has invalid upstream {hash_field}"
                )
        normalized.append(dict(record))
    return normalized


def _verify_recorded_artifact(
    run_path: Path,
    manifest: dict[str, Any],
    run_id: str,
    artifact_name: str,
) -> tuple[bytes, str]:
    path = run_path / artifact_name
    if not path.is_file():
        raise ArtifactError(
            f"quality-review input {run_id} has no {artifact_name}"
        )
    artifacts = manifest.get("artifacts")
    recorded = artifacts.get(artifact_name) if isinstance(artifacts, dict) else None
    if not isinstance(recorded, dict):
        raise ArtifactError(
            f"quality-review input {run_id} does not fingerprint {artifact_name}"
        )
    payload = path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if recorded.get("sha256") != digest:
        raise ArtifactError(
            f"quality-review input {run_id} {artifact_name} SHA-256 mismatch"
        )
    if recorded.get("bytes") != len(payload):
        raise ArtifactError(
            f"quality-review input {run_id} {artifact_name} byte count mismatch"
        )
    return payload, digest


def _verify_quality_review_decisions(
    run_path: Path,
    manifest: dict[str, Any],
    run_id: str,
) -> dict[str, TranslationProposal]:
    """Verify the selected result and structured GLM decision lineage."""

    _verify_recorded_artifact(run_path, manifest, run_id, "proposals.jsonl")
    _verify_recorded_artifact(run_path, manifest, run_id, "review-decisions.jsonl")
    try:
        selected = tuple(
            TranslationProposal.model_validate(item)
            for item in read_jsonl(run_path / "proposals.jsonl")
        )
        decisions = tuple(
            QualityReviewDecision.model_validate(item)
            for item in read_jsonl(run_path / "review-decisions.jsonl")
        )
    except ValidationError as exc:
        raise ArtifactError(
            f"quality-review input {run_id} has invalid GLM review lineage artifacts"
        ) from exc
    expected = manifest.get("quality_review_count")
    if (
        not selected
        or len(selected) != manifest.get("proposal_count")
        or len(decisions) != expected
        or len(decisions) != len(selected)
    ):
        raise ArtifactError(
            f"quality-review input {run_id} GLM review lineage counts do not match"
        )
    selected_by_key = {proposal.key: proposal for proposal in selected}
    if len(selected_by_key) != len(selected):
        raise ArtifactError(
            f"quality-review input {run_id} contains duplicate selected proposal keys"
        )
    for decision in decisions:
        proposal = selected_by_key.get(decision.key)
        if proposal is None or proposal.candidate_chinese != decision.result_candidate_chinese:
            raise ArtifactError(
                f"quality-review input {run_id} decision/result proposal mismatch"
            )
    return selected_by_key


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)
