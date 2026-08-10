"""Read-only, evidence-bounded state for the MagicForge Research Console.

The console is an observation surface, not a connectivity probe. Runtime
configuration is reported separately from audited bootstrap artifacts so a
configured GLM or Qdrant target is never presented as connected. Only the
explicit artifact allowlist below is read, and no filesystem path or secret is
included in the returned data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from app.config import PROJECT_ROOT, Settings
from app.runtime_corpus import ActiveCorpus
from knowledge.governance import MagicForgeMode


class ResearchConsoleReadModelError(RuntimeError):
    """Raised when an audited console artifact is missing or inconsistent."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "research_console_unavailable",
        status_code: int = 503,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


AlignmentStatus = Literal["aligned"]
PipelineStatus = Literal["completed", "receipt_verified"]
RunStatus = Literal["reported_succeeded", "receipt_verified"]
RunMetricBasis = Literal[
    "reported_generated_outputs",
    "receipt_verified_projections",
]


@dataclass(frozen=True, slots=True)
class PipelineStageObservation:
    id: str
    label: str
    status: PipelineStatus
    metrics: dict[str, int]


@dataclass(frozen=True, slots=True)
class RunHistoryObservation:
    run_id: str
    generated_at: str
    mode: str
    collection: str
    sources: int
    extracted_sources: int
    claims: int
    evidence_cards: int
    knowledge_nodes: int
    relationships: int
    qdrant_points: int
    extraction_errors: int
    status: RunStatus
    metric_basis: RunMetricBasis


@dataclass(frozen=True, slots=True)
class ResearchConsoleSnapshot:
    observed_at: str
    current_run: dict[str, Any]
    runtime: dict[str, Any]
    pipeline: dict[str, Any]
    memory_vault: dict[str, Any]
    governance: dict[str, Any]
    run_history: tuple[RunHistoryObservation, ...]


class ResearchConsoleReadModel:
    """Aggregate the three approved bootstrap reports without live probing."""

    _HISTORY_ARTIFACTS = {
        "bootstrap_001": Path("research/runs/bootstrap-001/run-report.json"),
        "bootstrap_001_v02": Path(
            "research/runs/bootstrap-001-v02/run-report.json"
        ),
    }

    def __init__(
        self,
        active_corpus: ActiveCorpus,
        project_root: str | Path = PROJECT_ROOT,
    ) -> None:
        if active_corpus.mode != MagicForgeMode.BOOTSTRAP:
            raise ResearchConsoleReadModelError(
                "the Bootstrap research console requires a Bootstrap active corpus"
            )
        if (
            active_corpus.run_summary_path is None
            or active_corpus.smoke_report_path is None
        ):
            raise ResearchConsoleReadModelError(
                "the active Bootstrap corpus is missing console artifacts"
            )
        self.active_corpus = active_corpus
        self._project_root = Path(project_root).resolve()
        self._reports: dict[str, Mapping[str, Any]] | None = None

    def snapshot(self, settings: Settings) -> ResearchConsoleSnapshot:
        artifacts = self._load()
        run_001 = artifacts["bootstrap_001"]
        run_001_v02 = artifacts["bootstrap_001_v02"]
        current = artifacts["bootstrap_002"]
        manifest = artifacts["bootstrap_002_manifest"]
        receipt = artifacts["bootstrap_002_receipt"]
        smoke = artifacts["bootstrap_002_smoke"]

        counts = _mapping(current, "counts", "bootstrap_002")
        safety = _mapping(current, "safety", "bootstrap_002")
        review = _mapping(current, "human_review_queue", "bootstrap_002")
        report_manifest = _mapping(current, "manifest", "bootstrap_002")

        manifest_id = _text(manifest, "id", "bootstrap_002_manifest")
        manifest_hash = _text(
            manifest, "manifest_hash", "bootstrap_002_manifest"
        )
        audited_collection = _text(
            manifest, "collection_name", "bootstrap_002_manifest"
        )
        projections = _list(manifest, "projections", "bootstrap_002_manifest")
        point_count = len(projections)

        receipt_points = _list(
            receipt, "point_ids", "bootstrap_002_receipt"
        )
        receipt_count = len(receipt_points)
        manifest_point_ids = _projection_point_ids(projections)
        receipt_point_ids = _string_list(
            receipt_points,
            "bootstrap_002_receipt.point_ids",
        )
        smoke_count = _integer(
            smoke, "collection_count", "bootstrap_002_smoke"
        )
        _verify_current_projection(
            current=current,
            report_manifest=report_manifest,
            manifest=manifest,
            receipt=receipt,
            smoke=smoke,
            manifest_id=manifest_id,
            manifest_hash=manifest_hash,
            collection=audited_collection,
            point_count=point_count,
            receipt_count=receipt_count,
            smoke_count=smoke_count,
            manifest_point_ids=manifest_point_ids,
            receipt_point_ids=receipt_point_ids,
            safety=safety,
        )
        _verify_active_corpus_identity(
            active_corpus=self.active_corpus,
            current=current,
            manifest=manifest,
            receipt=receipt,
            smoke=smoke,
            point_count=point_count,
        )

        runtime_collection = self.active_corpus.collection_name
        alignment: AlignmentStatus = "aligned"
        storage_kind = _console_storage_kind(self.active_corpus.storage_kind)

        pipeline_stages = _pipeline_stages(counts)
        runtime = {
            "api": {"status": "ok"},
            "intelligence_instrument": {
                "provider": "Zhipu GLM",
                "model": settings.glm_model,
                "configured": bool(settings.glm_api_key),
                "connectivity": "not_probed",
                "structured_extraction": True,
            },
            "retrieval": {
                "configured": True,
                "collection": runtime_collection,
                "storage_kind": storage_kind,
                "connectivity": "not_probed",
            },
        }
        memory_vault = {
            "runtime_collection": runtime_collection,
            "audited_collection": audited_collection,
            "alignment_status": alignment,
            "manifest": {
                "status": "manifest_verified",
                "id": manifest_id,
                "hash": manifest_hash,
                "point_count": point_count,
            },
            "receipt": {
                "status": "receipt_verified",
                "id": _text(receipt, "id", "bootstrap_002_receipt"),
                "ingested_at": _text(
                    receipt, "ingested_at", "bootstrap_002_receipt"
                ),
                "point_count": receipt_count,
            },
            "retrieval_smoke": {
                "status": "report_verified",
                "tested_at": _text(smoke, "tested_at", "bootstrap_002_smoke"),
                "query_count": len(
                    _mapping(smoke, "queries", "bootstrap_002_smoke")
                ),
                "collection_count": smoke_count,
                "all_returned_hits_bootstrap_safe": _boolean(
                    smoke,
                    "all_returned_hits_bootstrap_safe",
                    "bootstrap_002_smoke",
                ),
            },
            "points": {
                "manifest": point_count,
                "receipt": receipt_count,
                "smoke_observed": smoke_count,
            },
            "safety": {
                "bootstrap_generated_points": _integer(
                    safety, "bootstrap_generated_points", "bootstrap_002.safety"
                ),
                "human_verified_points": _integer(
                    safety, "human_verified_points", "bootstrap_002.safety"
                ),
                "approved_points": _integer(
                    safety, "approved_points", "bootstrap_002.safety"
                ),
                "storage_permission_points": _integer(
                    safety, "storage_permission_points", "bootstrap_002.safety"
                ),
                "production_collection_touched": _boolean(
                    safety,
                    "production_collection_touched",
                    "bootstrap_002.safety",
                ),
                "production_collection_present_in_smoke": _boolean(
                    smoke,
                    "production_collection_present",
                    "bootstrap_002_smoke",
                ),
                "safety_excluded_projection_count": _integer(
                    safety,
                    "safety_excluded_projection_count",
                    "bootstrap_002.safety",
                ),
            },
        }
        governance = {
            "mode": _text(current, "mode", "bootstrap_002"),
            "checkpoint_status": "pending_human_review",
            "sources_pending": _integer(
                review, "sources", "bootstrap_002.human_review_queue"
            ),
            "evidence_cards_pending": _integer(
                review, "evidence_cards", "bootstrap_002.human_review_queue"
            ),
            "knowledge_nodes_pending": _integer(
                review, "knowledge_nodes", "bootstrap_002.human_review_queue"
            ),
            "relationships_pending": _integer(
                review, "relationships", "bootstrap_002.human_review_queue"
            ),
            "contradiction_checks_pending": _integer(
                review,
                "contradiction_checks_pending",
                "bootstrap_002.human_review_queue",
            ),
            "procedural_method_projections_quarantined": _integer(
                review,
                "procedural_method_projections_quarantined",
                "bootstrap_002.human_review_queue",
            ),
            "human_verified_points": _integer(
                safety, "human_verified_points", "bootstrap_002.safety"
            ),
            "approved_points": _integer(
                safety, "approved_points", "bootstrap_002.safety"
            ),
            "storage_permission_points": _integer(
                safety, "storage_permission_points", "bootstrap_002.safety"
            ),
        }

        return ResearchConsoleSnapshot(
            observed_at=_utc_now(),
            current_run={
                "run_id": self.active_corpus.corpus_id,
                "mode": self.active_corpus.mode.value,
                "generated_at": _text(
                    current, "generated_at", "bootstrap_002"
                ),
                "collection": self.active_corpus.collection_name,
                "status": "receipt_verified",
            },
            runtime=runtime,
            pipeline={
                "status": "receipt_verified",
                "stages": pipeline_stages,
            },
            memory_vault=memory_vault,
            governance=governance,
            run_history=(
                _history_item(run_001, "bootstrap_001"),
                _history_item(run_001_v02, "bootstrap_001_v02"),
                _history_item(current, "bootstrap_002", receipt_verified=True),
            ),
        )

    def _load(self) -> dict[str, Mapping[str, Any]]:
        if self._reports is not None:
            return self._reports
        run_summary_path = self.active_corpus.run_summary_path
        smoke_report_path = self.active_corpus.smoke_report_path
        assert run_summary_path is not None  # checked by the constructor
        assert smoke_report_path is not None  # checked by the constructor
        reports: dict[str, Mapping[str, Any]] = {
            "bootstrap_002": _read_json(
                run_summary_path,
                "bootstrap_002",
            ),
            "bootstrap_002_manifest": _read_json(
                self.active_corpus.manifest_path,
                "bootstrap_002_manifest",
            ),
            "bootstrap_002_receipt": _read_json(
                self.active_corpus.receipt_path,
                "bootstrap_002_receipt",
            ),
            "bootstrap_002_smoke": _read_json(
                smoke_report_path,
                "bootstrap_002_smoke",
            ),
        }
        for label, relative_path in self._HISTORY_ARTIFACTS.items():
            reports[label] = _read_json(
                self._project_root / relative_path,
                label,
            )
        self._reports = reports
        return reports


def _verify_active_corpus_identity(
    *,
    active_corpus: ActiveCorpus,
    current: Mapping[str, Any],
    manifest: Mapping[str, Any],
    receipt: Mapping[str, Any],
    smoke: Mapping[str, Any],
    point_count: int,
) -> None:
    """Prove the console artifacts are the descriptor-selected corpus."""

    expected_values = {
        "run id": (_text(current, "run_id", "bootstrap_002"), active_corpus.corpus_id),
        "run mode": (
            _text(current, "mode", "bootstrap_002"),
            active_corpus.mode.value,
        ),
        "run collection": (
            _text(current, "collection", "bootstrap_002"),
            active_corpus.collection_name,
        ),
        "manifest schema": (
            _text(manifest, "schema_version", "bootstrap_002_manifest"),
            active_corpus.manifest_schema_version,
        ),
        "manifest id": (
            _text(manifest, "id", "bootstrap_002_manifest"),
            active_corpus.manifest_id,
        ),
        "manifest hash": (
            _text(manifest, "manifest_hash", "bootstrap_002_manifest"),
            active_corpus.manifest_hash,
        ),
        "manifest collection": (
            _text(manifest, "collection_name", "bootstrap_002_manifest"),
            active_corpus.collection_name,
        ),
        "receipt id": (
            _text(receipt, "id", "bootstrap_002_receipt"),
            active_corpus.receipt_id,
        ),
        "receipt manifest id": (
            _text(receipt, "manifest_id", "bootstrap_002_receipt"),
            active_corpus.manifest_id,
        ),
        "receipt manifest hash": (
            _text(receipt, "manifest_hash", "bootstrap_002_receipt"),
            active_corpus.manifest_hash,
        ),
        "receipt collection": (
            _text(receipt, "collection_name", "bootstrap_002_receipt"),
            active_corpus.collection_name,
        ),
        "smoke collection": (
            _text(smoke, "collection", "bootstrap_002_smoke"),
            active_corpus.collection_name,
        ),
        "point count": (point_count, active_corpus.expected_point_count),
    }
    for name, (observed, expected) in expected_values.items():
        if observed != expected:
            raise ResearchConsoleReadModelError(
                f"active corpus mismatch: {name}"
            )


def _console_storage_kind(value: object) -> Literal["local", "remote"]:
    raw = getattr(value, "value", value)
    normalized = str(raw).strip().casefold()
    mapping: dict[str, Literal["local", "remote"]] = {
        "local": "local",
        "embedded": "local",
        "remote": "remote",
        "server": "remote",
    }
    try:
        return mapping[normalized]
    except KeyError as exc:
        raise ResearchConsoleReadModelError(
            "active corpus contains an unsupported Qdrant storage kind"
        ) from exc


def _pipeline_stages(
    counts: Mapping[str, Any],
) -> tuple[PipelineStageObservation, ...]:
    label = "bootstrap_002.counts"
    return (
        PipelineStageObservation(
            id="discovery",
            label="Discovery chamber",
            status="completed",
            metrics={
                "queries_executed": _integer(counts, "queries_executed", label),
                "raw_results": _integer(counts, "raw_discovery_results", label),
                "verified_new_sources": _integer(
                    counts, "verified_new_sources", label
                ),
            },
        ),
        PipelineStageObservation(
            id="source_registry",
            label="Source registry",
            status="completed",
            metrics={
                "sources_processed": _integer(
                    counts, "sources_processed_total", label
                )
            },
        ),
        PipelineStageObservation(
            id="extraction",
            label="Extraction engine",
            status="completed",
            metrics={
                "sources_extracted": _integer(
                    counts, "sources_extracted_successfully", label
                ),
                "claims_generated": _integer(counts, "claims_generated", label),
                "extraction_errors": _integer(counts, "extraction_errors", label),
            },
        ),
        PipelineStageObservation(
            id="evidence_forge",
            label="Evidence forge",
            status="completed",
            metrics={
                "generated": _integer(counts, "evidence_cards_generated", label),
                "projected": _integer(counts, "evidence_cards_projected", label),
            },
        ),
        PipelineStageObservation(
            id="knowledge_assembly",
            label="Knowledge assembly",
            status="completed",
            metrics={
                "generated": _integer(counts, "knowledge_nodes_generated", label),
                "projected": _integer(counts, "knowledge_nodes_projected", label),
                "semantic_rejections": _integer(
                    counts, "semantic_proposals_rejected", label
                ),
            },
        ),
        PipelineStageObservation(
            id="relationship_gate",
            label="Relationship entailment gate",
            status="completed",
            metrics={
                "generated": _integer(counts, "relationships_generated", label),
                "projected": _integer(counts, "relationships_projected", label),
            },
        ),
        PipelineStageObservation(
            id="memory_projection",
            label="Memory projection",
            status="receipt_verified",
            metrics={
                "qdrant_points": _integer(counts, "qdrant_points_created", label)
            },
        ),
    )


def _history_item(
    report: Mapping[str, Any],
    label: str,
    *,
    receipt_verified: bool = False,
) -> RunHistoryObservation:
    if label == "bootstrap_002":
        counts = _mapping(report, "counts", label)
        return RunHistoryObservation(
            run_id=_text(report, "run_id", label),
            generated_at=_text(report, "generated_at", label),
            mode=_text(report, "mode", label),
            collection=_text(report, "collection", label),
            sources=_integer(counts, "sources_processed_total", f"{label}.counts"),
            extracted_sources=_integer(
                counts, "sources_extracted_successfully", f"{label}.counts"
            ),
            claims=_integer(counts, "claims_generated", f"{label}.counts"),
            evidence_cards=_integer(
                counts, "evidence_cards_projected", f"{label}.counts"
            ),
            knowledge_nodes=_integer(
                counts, "knowledge_nodes_projected", f"{label}.counts"
            ),
            relationships=_integer(
                counts, "relationships_projected", f"{label}.counts"
            ),
            qdrant_points=_integer(
                counts, "qdrant_points_created", f"{label}.counts"
            ),
            extraction_errors=_integer(
                counts, "extraction_errors", f"{label}.counts"
            ),
            status="receipt_verified" if receipt_verified else "reported_succeeded",
            metric_basis="receipt_verified_projections",
        )

    safety = _mapping(report, "safety", label)
    if _boolean(report, "qdrant_ingestion_succeeded", label) is not True:
        raise ResearchConsoleReadModelError(
            f"research artifact {label} does not report successful ingestion"
        )
    return RunHistoryObservation(
        run_id=_text(report, "run_id", label),
        generated_at=_text(report, "generated_at", label),
        mode=_text(report, "mode", label),
        collection=_text(safety, "bootstrap_collection", f"{label}.safety"),
        sources=_integer(report, "sources_processed", label),
        extracted_sources=_integer(
            report, "sources_extracted_successfully", label
        ),
        claims=_integer(report, "claims_generated", label),
        evidence_cards=_integer(report, "evidence_cards_generated", label),
        knowledge_nodes=_integer(report, "knowledge_nodes_generated", label),
        relationships=_integer(report, "relationships_generated", label),
        qdrant_points=_integer(report, "qdrant_points_created", label),
        extraction_errors=_integer(report, "extraction_errors", label),
        status="reported_succeeded",
        metric_basis="reported_generated_outputs",
    )


def _verify_current_projection(
    *,
    current: Mapping[str, Any],
    report_manifest: Mapping[str, Any],
    manifest: Mapping[str, Any],
    receipt: Mapping[str, Any],
    smoke: Mapping[str, Any],
    manifest_id: str,
    manifest_hash: str,
    collection: str,
    point_count: int,
    receipt_count: int,
    smoke_count: int,
    manifest_point_ids: Sequence[str],
    receipt_point_ids: Sequence[str],
    safety: Mapping[str, Any],
) -> None:
    counts = _mapping(current, "counts", "bootstrap_002")
    expected_values = {
        "report manifest id": (
            _text(report_manifest, "id", "bootstrap_002.manifest"),
            manifest_id,
        ),
        "report manifest hash": (
            _text(report_manifest, "hash", "bootstrap_002.manifest"),
            manifest_hash,
        ),
        "receipt manifest id": (
            _text(receipt, "manifest_id", "bootstrap_002_receipt"),
            manifest_id,
        ),
        "receipt manifest hash": (
            _text(receipt, "manifest_hash", "bootstrap_002_receipt"),
            manifest_hash,
        ),
        "receipt collection": (
            _text(receipt, "collection_name", "bootstrap_002_receipt"),
            collection,
        ),
        "smoke collection": (
            _text(smoke, "collection", "bootstrap_002_smoke"),
            collection,
        ),
        "run collection": (
            _text(current, "collection", "bootstrap_002"),
            collection,
        ),
        "report receipt id": (
            _text(report_manifest, "receipt_id", "bootstrap_002.manifest"),
            _text(receipt, "id", "bootstrap_002_receipt"),
        ),
    }
    for name, (observed, expected) in expected_values.items():
        if observed != expected:
            raise ResearchConsoleReadModelError(
                f"audited projection mismatch: {name}"
            )

    counts_to_verify = {
        "run summary": _integer(
            counts, "qdrant_points_created", "bootstrap_002.counts"
        ),
        "run manifest declaration": _integer(
            report_manifest, "point_count", "bootstrap_002.manifest"
        ),
        "manifest": point_count,
        "receipt": receipt_count,
        "retrieval smoke": smoke_count,
    }
    if set(counts_to_verify.values()) != {point_count}:
        raise ResearchConsoleReadModelError(
            "audited projection point counts are not aligned"
        )
    if len(set(manifest_point_ids)) != point_count:
        raise ResearchConsoleReadModelError(
            "audited manifest contains duplicate point identifiers"
        )
    if len(set(receipt_point_ids)) != receipt_count:
        raise ResearchConsoleReadModelError(
            "audited receipt contains duplicate point identifiers"
        )
    if set(manifest_point_ids) != set(receipt_point_ids):
        raise ResearchConsoleReadModelError(
            "audited receipt point identifiers do not match the manifest"
        )
    if _integer(
        safety,
        "bootstrap_generated_points",
        "bootstrap_002.safety",
    ) != point_count:
        raise ResearchConsoleReadModelError(
            "audited bootstrap point count does not match the manifest"
        )
    if _boolean(receipt, "bootstrap_generated", "bootstrap_002_receipt") is not True:
        raise ResearchConsoleReadModelError(
            "audited receipt is missing its bootstrap marker"
        )
    if _boolean(receipt, "human_verified", "bootstrap_002_receipt") is not False:
        raise ResearchConsoleReadModelError(
            "audited receipt incorrectly claims human verification"
        )
    if _boolean(
        smoke,
        "all_returned_hits_bootstrap_safe",
        "bootstrap_002_smoke",
    ) is not True:
        raise ResearchConsoleReadModelError(
            "retrieval smoke report did not pass bootstrap safety"
        )
    if _integer(
        smoke,
        "safety_excluded_points_found",
        "bootstrap_002_smoke",
    ) != 0:
        raise ResearchConsoleReadModelError(
            "retrieval smoke report returned safety-excluded points"
        )
    if _boolean(
        smoke,
        "production_collection_present",
        "bootstrap_002_smoke",
    ) is not False:
        raise ResearchConsoleReadModelError(
            "retrieval smoke report found the production collection"
        )


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchConsoleReadModelError(
            f"could not read approved research artifact: {label}"
        ) from exc
    if not isinstance(value, dict):
        raise ResearchConsoleReadModelError(
            f"approved research artifact is malformed: {label}"
        )
    return value


def _mapping(value: Mapping[str, Any], key: str, label: str) -> Mapping[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise ResearchConsoleReadModelError(
            f"research artifact field is malformed: {label}.{key}"
        )
    return result


def _list(value: Mapping[str, Any], key: str, label: str) -> list[Any]:
    result = value.get(key)
    if not isinstance(result, list):
        raise ResearchConsoleReadModelError(
            f"research artifact field is malformed: {label}.{key}"
        )
    return result


def _projection_point_ids(projections: Sequence[Any]) -> tuple[str, ...]:
    point_ids: list[str] = []
    for index, projection in enumerate(projections):
        if not isinstance(projection, dict):
            raise ResearchConsoleReadModelError(
                "research artifact field is malformed: "
                f"bootstrap_002_manifest.projections[{index}]"
            )
        point_ids.append(
            _text(
                projection,
                "knowledge_unit_id",
                f"bootstrap_002_manifest.projections[{index}]",
            )
        )
    return tuple(point_ids)


def _string_list(values: Sequence[Any], label: str) -> tuple[str, ...]:
    result: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise ResearchConsoleReadModelError(
                f"research artifact field is malformed: {label}[{index}]"
            )
        result.append(value)
    return tuple(result)


def _text(value: Mapping[str, Any], key: str, label: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ResearchConsoleReadModelError(
            f"research artifact field is malformed: {label}.{key}"
        )
    return result


def _integer(value: Mapping[str, Any], key: str, label: str) -> int:
    result = value.get(key)
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise ResearchConsoleReadModelError(
            f"research artifact field is malformed: {label}.{key}"
        )
    return result


def _boolean(value: Mapping[str, Any], key: str, label: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise ResearchConsoleReadModelError(
            f"research artifact field is malformed: {label}.{key}"
        )
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
