"""Execute the isolated bootstrap corpus run from registered source records."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from qdrant_client import QdrantClient

from app.config import get_settings
from knowledge.bootstrap import (
    BOOTSTRAP_COLLECTION_NAME,
    BootstrapKnowledgeNodeCandidate,
    BootstrapProjectionBuilder,
    BootstrapRelationshipCandidate,
    BootstrapStorageManifest,
)
from knowledge.evidence import ClaimRole, EvidenceCard, KnowledgeOrigin
from knowledge.governance import MagicForgeMode
from llm.glm_client import GLMClient
from research.bootstrap.service import (
    BootstrapKnowledgeAssembler,
    BootstrapSourceRegistrar,
    canonicalize_bootstrap_artifacts,
    source_review_item_id,
)
from research.citation.models import CitationRecord
from research.extraction.extractor import ResearchKnowledgeExtractor
from research.extraction.models import KnowledgeProposal
from research.extraction.quality import normalize_bootstrap_evidence_card
from research.models import ContentAccess, ResearchCandidate, SourceCategory
from retrieval.bootstrap_qdrant import BootstrapQdrantWriter
from retrieval.embeddings import FastEmbedProvider
from retrieval.qdrant_service import QdrantService


DEFAULT_SOURCE_RUN = Path("research/runs/magicforge-corpus-run-001")
DEFAULT_OUTPUT = Path("research/runs/bootstrap-001")
DEFAULT_CONTENT_BUNDLE = Path("/tmp/magicforge-bootstrap-source-content.json")
CONTENT_SELECTION_VERSION = "section-window-0.2"
SEMANTIC_PIPELINE_VERSION = "bootstrap-extraction-0.2.3"
MIGRATABLE_SEMANTIC_PIPELINE_VERSIONS = frozenset(
    {"bootstrap-extraction-0.2.2"}
)
SUPERSEDED_PREPARED_INPUT_LEDGERS = frozenset(
    {
        # Practitioner audit v0.2 replaced this batch after stricter exclusions.
        "e4830a43cf8853ab5aadde246335a3ba5ec07284735531d5ac7398361cb7c590",
        # Academic metadata correction changed the canonical publication year.
        "2996b5e8d888ad3508fc2edda9ab4ea31c05d33bfab6ef1c16c8f8bd3a2d2027",
        # Bootstrap-004 practitioner bundle was regenerated with its final
        # exact-content provenance envelope; retain the older ledger only as
        # immutable acquisition history.
        "9327cce7518d4e21ec696eb0e4c3efd7824b0dc1c97e9ba3917ea3481bfa7400",
        # Bootstrap-006 relationship preparation originally included the
        # Mystery Card's hidden optical implementation.  The Source remains
        # discovery/review material, but this prepared practitioner batch must
        # never reach GLM after the restricted-detail safety narrowing.
        "4f93818e16ec090f31a08cc5b2ed9dd91ae45991801cbae52d8d38247cd0357a",
    }
)
_PRACTITIONER_POLICY_ROLES = frozenset(
    {ClaimRole.EXPERT_OPINION, ClaimRole.CONTEXT_ONLY}
)
_ACADEMIC_POLICY_ROLES = frozenset(
    {
        ClaimRole.RESULT,
        ClaimRole.METHOD,
        ClaimRole.BACKGROUND,
        ClaimRole.HYPOTHESIS,
        ClaimRole.DISCUSSION,
        ClaimRole.CONTEXT_ONLY,
    }
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, default=DEFAULT_SOURCE_RUN)
    parser.add_argument(
        "--additional-source-run",
        type=Path,
        action="append",
        default=[],
        help="Additional discovery run(s) to merge after deterministic deduplication.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--content-bundle", type=Path, default=DEFAULT_CONTENT_BUNDLE)
    parser.add_argument(
        "--additional-content-bundle",
        type=Path,
        action="append",
        default=[],
        help="Additional temporary MCP content bundle(s).",
    )
    parser.add_argument("--skip-qdrant", action="store_true")
    parser.add_argument(
        "--force-reprocess",
        action="store_true",
        help="Ignore cached per-source artifacts and run extraction again.",
    )
    parser.add_argument("--max-source-characters", type=int, default=8_000)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=0.0,
        help="Delay before each uncached GLM source request to respect account limits.",
    )
    parser.add_argument(
        "--reprocess-candidate-id",
        action="append",
        default=[],
        help="Reprocess only these candidate IDs while retaining all other valid caches.",
    )
    args = parser.parse_args()
    run(
        source_run=args.source_run,
        additional_source_runs=args.additional_source_run,
        output=args.output,
        content_bundle=args.content_bundle,
        additional_content_bundles=args.additional_content_bundle,
        ingest_qdrant=not args.skip_qdrant,
        max_source_characters=args.max_source_characters,
        workers=args.workers,
        force_reprocess=args.force_reprocess,
        request_interval_seconds=args.request_interval_seconds,
        reprocess_candidate_ids=set(args.reprocess_candidate_id),
    )


def run(
    *,
    source_run: Path,
    additional_source_runs: list[Path] | None = None,
    output: Path,
    content_bundle: Path,
    additional_content_bundles: list[Path] | None = None,
    ingest_qdrant: bool,
    max_source_characters: int,
    workers: int = 3,
    force_reprocess: bool = False,
    request_interval_seconds: float = 0.0,
    reprocess_candidate_ids: set[str] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if settings.magicforge_mode != MagicForgeMode.BOOTSTRAP:
        raise RuntimeError(
            "bootstrap runner is disabled; set MAGICFORGE_MODE=bootstrap"
        )
    if settings.qdrant_bootstrap_collection_name != BOOTSTRAP_COLLECTION_NAME:
        raise RuntimeError(
            f"bootstrap collection must remain {BOOTSTRAP_COLLECTION_NAME}"
        )
    if not settings.glm_api_key:
        raise RuntimeError("GLM_API_KEY is required for bootstrap extraction")

    directories = [
        output / "sources",
        output / "extracted_claims",
        output / "evidence_cards",
        output / "knowledge_nodes",
        output / "qdrant_manifest",
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    source_runs = [source_run, *(additional_source_runs or [])]
    input_lineage = []
    for run_path in source_runs:
        lineage = _verify_prepared_input_manifest(run_path, output)
        if lineage is not None:
            input_lineage.append(lineage)
    candidates = _deduplicate_candidates(
        candidate
        for run_path in source_runs
        for candidate in _load_candidates(run_path)
    )
    source_records = _deduplicate_source_records(
        record
        for run_path in source_runs
        for record in _load_source_records(run_path)
    )
    content = _load_content(content_bundle)
    for bundle in additional_content_bundles or []:
        content.update(_load_content(bundle))
    citation_by_candidate = {
        item["candidate_id"]: CitationRecord.model_validate(item["citation"])
        for item in source_records
    }
    source_claim_policies = _load_source_claim_policies(source_runs)
    unknown_policy_ids = set(source_claim_policies) - {item.id for item in candidates}
    if unknown_policy_ids:
        raise ValueError(
            "source claim policy refers to unknown candidate(s): "
            + ", ".join(sorted(unknown_policy_ids))
        )

    projection_builder = BootstrapProjectionBuilder()

    all_cards = {}
    all_nodes = {}
    all_relationships = {}
    context_only_claim_count = 0
    semantic_rejections = []
    processed_sources = []
    extraction_errors = []
    later_review = []
    quality_corrections_path = output / "quality-corrections.json"
    quality_corrections = (
        json.loads(quality_corrections_path.read_text(encoding="utf-8"))
        if quality_corrections_path.exists()
        else []
    )
    if not isinstance(quality_corrections, list):
        raise ValueError("quality correction ledger must be a JSON array")

    worker_count = max(1, min(workers, 6))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _process_candidate,
                sequence=sequence,
                candidate=candidate,
                citation=citation_by_candidate[candidate.id],
                acquired=_content_for(candidate, content),
                output=output,
                mode=settings.magicforge_mode,
                glm_api_key=settings.glm_api_key,
                glm_model=settings.glm_model,
                glm_timeout_seconds=settings.glm_timeout_seconds,
                glm_max_retries=settings.glm_max_retries,
                max_source_characters=max_source_characters,
                force_reprocess=force_reprocess,
                reprocess_candidate=(
                    candidate.id in (reprocess_candidate_ids or set())
                ),
                request_interval_seconds=request_interval_seconds,
                source_claim_policy=source_claim_policies.get(candidate.id),
            ): candidate
            for sequence, candidate in enumerate(candidates, start=1)
        }
        completed = 0
        for future in as_completed(futures):
            result = future.result()
            completed += 1
            processed_sources.append(result["source"])
            later_review.append(result["later_review"])
            if result["error"]:
                extraction_errors.append(result["error"])
            all_cards.update({item.id: item for item in result["cards"]})
            all_nodes.update({item.id: item for item in result["nodes"]})
            all_relationships.update(
                {item.id: item for item in result["relationships"]}
            )
            context_only_claim_count += result["context_only_claims"]
            semantic_rejections.extend(result["semantic_rejections"])
            quality_corrections.extend(result["quality_corrections"])
            print(
                json.dumps(
                    {
                        "completed": completed,
                        "total": len(candidates),
                        "title": result["source"]["title"],
                        "cards": len(result["cards"]),
                        "nodes": len(result["nodes"]),
                        "error": result["error"]["error_type"]
                        if result["error"]
                        else None,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    raw_node_count = len(all_nodes)
    raw_relationship_count = len(all_relationships)
    canonical_nodes, canonical_relationships = canonicalize_bootstrap_artifacts(
        list(all_nodes.values()), list(all_relationships.values())
    )
    all_nodes = {item.id: item for item in canonical_nodes}
    all_relationships = {item.id: item for item in canonical_relationships}
    _dump(
        output / "knowledge_nodes" / "_canonical_registry.json",
        {
            "nodes": [item.model_dump(mode="json") for item in canonical_nodes],
            "relationships": [
                item.model_dump(mode="json") for item in canonical_relationships
            ],
            "merge_statistics": {
                "node_proposals_before": raw_node_count,
                "canonical_nodes_after": len(canonical_nodes),
                "relationship_proposals_before": raw_relationship_count,
                "canonical_relationships_after": len(canonical_relationships),
            },
        },
    )

    projections = [
        *(
            projection_builder.from_evidence_card(card)
            for card in all_cards.values()
        ),
        *(
            projection_builder.from_node(node, all_cards)
            for node in all_nodes.values()
        ),
        *(
            projection_builder.from_relationship(item, all_cards)
            for item in all_relationships.values()
        ),
    ]
    manifest = (
        BootstrapStorageManifest(projections=projections) if projections else None
    )
    if manifest is not None:
        _dump(
            output / "qdrant_manifest" / "bootstrap-manifest.json",
            manifest.model_dump(mode="json"),
        )
    else:
        _dump(
            output / "qdrant_manifest" / "projection-status.json",
            {
                "status": "not_created",
                "reason": "No Evidence Card, Knowledge Node, or Relationship projection passed validation.",
                "qdrant_ingestion_attempted": False,
            },
        )
    _dump(output / "sources" / "later-human-review.json", later_review)
    _dump(output / "extraction-errors.json", extraction_errors)
    _dump(output / "semantic-rejections.json", semantic_rejections)
    quality_corrections = _unique_dicts(quality_corrections)
    _dump(quality_corrections_path, quality_corrections)

    receipt = None
    qdrant_error = None
    if ingest_qdrant and manifest is None:
        qdrant_error = {
            "error_type": "EmptyProjectionSet",
            "error": "Qdrant ingestion was not attempted because no validated projections exist.",
        }
        _dump(
            output / "qdrant_manifest" / "ingestion-error.json",
            qdrant_error,
        )
    elif ingest_qdrant:
        try:
            qdrant = QdrantService(
                url=settings.qdrant_url,
                collection_name=settings.qdrant_bootstrap_collection_name,
                embedding_provider=FastEmbedProvider(
                    settings.embedding_model,
                    settings.embedding_dimension,
                ),
                client=(
                    QdrantClient(path=settings.qdrant_bootstrap_local_path)
                    if settings.qdrant_bootstrap_local_path
                    else None
                ),
                mode=MagicForgeMode.BOOTSTRAP,
            )
            receipt = BootstrapQdrantWriter(qdrant).write_manifest(manifest)
            _dump(
                output / "qdrant_manifest" / "ingestion-receipt.json",
                receipt.model_dump(mode="json"),
            )
        except Exception as exc:
            qdrant_error = {
                "error_type": type(exc).__name__,
                "error": _safe_error(exc),
            }
            _dump(
                output / "qdrant_manifest" / "ingestion-error.json",
                qdrant_error,
            )

    card_source_ids = {
        card.source.source_candidate_id for card in all_cards.values()
    }
    unreadable_sources = sum(
        item.get("content_access") != ContentAccess.WEB_EXTRACT.value
        for item in processed_sources
    )
    sources_without_cards = len(processed_sources) - len(card_source_ids)
    research_gaps = [
        f"{unreadable_sources} registered source(s) lack readable exact content.",
        f"{sources_without_cards} readable source(s) produced no Evidence Card that passed semantic validation.",
        "Relationship recall is intentionally conservative because only explicitly entailed edges survive the gate.",
        "All contradiction checks remain pending human review, and bootstrap confidence is intentionally conservative.",
    ]
    if not any(
        card.evidence_class.value in {"systematic_review", "meta_analysis"}
        for card in all_cards.values()
    ):
        research_gaps.insert(
            2,
            "The corpus currently contains no systematic review or meta-analysis Evidence Cards.",
        )

    report = {
        "run_id": output.name,
        "mode": "bootstrap",
        "generated_at": datetime.now(UTC).isoformat(),
        "input_lineage": input_lineage,
        "sources_processed": len(processed_sources),
        "source_breakdown": {
            "existing_discovery_sources": len(_load_candidates(source_run)),
            "additional_existing_sources": len(processed_sources)
            - len(_load_candidates(source_run)),
        },
        "sources_with_readable_content": sum(
            item.get("content_access") == ContentAccess.WEB_EXTRACT.value
            for item in processed_sources
        ),
        "sources_extracted_successfully": sum(
            item.get("extraction_status") == "completed"
            for item in processed_sources
        ),
        "claims_generated": len(all_cards) + context_only_claim_count,
        "context_only_claims_generated": context_only_claim_count,
        "evidence_cards_generated": len(all_cards),
        "knowledge_nodes_generated": len(all_nodes),
        "relationships_generated": len(all_relationships),
        "semantic_rejections": len(semantic_rejections),
        "quality_corrections": {
            "total": len(quality_corrections),
            "by_reason": dict(
                Counter(
                    reason
                    for correction in quality_corrections
                    for reason in correction.get("reasons", [])
                )
            ),
        },
        "canonical_merge": {
            "node_proposals_before": raw_node_count,
            "canonical_nodes_after": len(all_nodes),
            "relationship_proposals_before": raw_relationship_count,
            "canonical_relationships_after": len(all_relationships),
        },
        "qdrant_points_prepared": manifest.expected_point_count if manifest else 0,
        "qdrant_points_created": len(receipt.point_ids) if receipt else 0,
        "qdrant_ingestion_attempted": ingest_qdrant,
        "qdrant_ingestion_succeeded": receipt is not None,
        "sources_requiring_later_human_review": len(later_review),
        "extraction_errors": len(extraction_errors),
        "qdrant_error": qdrant_error,
        "knowledge_origin_counts": dict(
            Counter(card.knowledge_origin.value for card in all_cards.values())
        ),
        "evidence_class_counts": dict(
            Counter(card.evidence_class.value for card in all_cards.values())
        ),
        "claim_role_counts": dict(
            Counter(card.claim_role.value for card in all_cards.values())
        ),
        "research_gaps": research_gaps,
        "human_review_items": {
            "sources": len(later_review),
            "evidence_cards": len(all_cards),
            "knowledge_nodes": len(all_nodes),
            "relationships": len(all_relationships),
            "contradiction_checks_pending": sum(
                card.contradiction_status.value == "not_checked"
                for card in all_cards.values()
            ),
        },
        "safety": {
            "production_collection_touched": False,
            "production_collection": settings.qdrant_collection_name,
            "bootstrap_collection": BOOTSTRAP_COLLECTION_NAME,
            "all_cards_human_verified": False,
            "all_nodes_verification_status": "unverified",
        },
    }
    _dump(output / "run-report.json", report)
    (output / "run_report.md").write_text(_render_report(report), encoding="utf-8")
    return report


def _process_candidate(
    *,
    sequence: int,
    candidate: ResearchCandidate,
    citation: CitationRecord,
    acquired: str,
    output: Path,
    mode: MagicForgeMode,
    glm_api_key: str,
    glm_model: str,
    glm_timeout_seconds: float,
    glm_max_retries: int,
    max_source_characters: int,
    force_reprocess: bool,
    reprocess_candidate: bool,
    request_interval_seconds: float,
    source_claim_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_policy_hash = (
        _canonical_json_hash(source_claim_policy) if source_claim_policy else None
    )
    base_source = {
        "sequence": sequence,
        "candidate_id": candidate.id,
        "citation_id": citation.id,
        "title": candidate.title,
        "url": candidate.url,
        "doi": candidate.doi,
        "source_category": candidate.source_category.value,
        "source_review_status": "bootstrap_pending_human_review",
        "bootstrap_generated": True,
        "human_verified": False,
        "content_selection_version": CONTENT_SELECTION_VERSION,
        "semantic_pipeline_version": SEMANTIC_PIPELINE_VERSION,
        "source_claim_policy_hash": source_policy_hash,
        "provenance": [item.model_dump(mode="json") for item in candidate.provenance],
    }
    if not acquired:
        record = {
            **base_source,
            "content_access": "unavailable",
            "extraction_status": "not_extracted",
            "reason": "No readable MCP extract was available in this run.",
        }
        _dump(output / "sources" / f"{candidate.id}.json", record)
        return {
            "source": record,
            "later_review": _later_review(record, None),
            "cards": [],
            "nodes": [],
            "relationships": [],
            "context_only_claims": 0,
            "semantic_rejections": [],
            "quality_corrections": [],
            "error": None,
        }

    acquired = _prepare_source_content(
        acquired,
        candidate,
        max_characters=max_source_characters,
    )
    enriched = candidate.model_copy(
        update={
            "content": acquired,
            "content_access": ContentAccess.WEB_EXTRACT,
        }
    )
    source_approval = BootstrapSourceRegistrar(mode).register(enriched, citation)
    existing_source_path = output / "sources" / f"{candidate.id}.json"
    existing_cards_path = output / "evidence_cards" / f"{candidate.id}.json"
    existing_nodes_path = output / "knowledge_nodes" / f"{candidate.id}.json"
    if (
        not force_reprocess
        and not reprocess_candidate
        and
        existing_source_path.exists()
        and existing_cards_path.exists()
        and existing_nodes_path.exists()
    ):
        existing_source = json.loads(existing_source_path.read_text(encoding="utf-8"))
        if (
            existing_source.get("extraction_status") == "completed"
            and existing_source.get("content_selection_version")
            == CONTENT_SELECTION_VERSION
            and existing_source.get("semantic_pipeline_version")
            in {
                SEMANTIC_PIPELINE_VERSION,
                *MIGRATABLE_SEMANTIC_PIPELINE_VERSIONS,
            }
            and existing_source.get("source_claim_policy_hash")
            == source_policy_hash
        ):
            cards = TypeAdapter(list[EvidenceCard]).validate_json(
                existing_cards_path.read_text(encoding="utf-8")
            )
            quality_corrections = []
            normalized_cards = []
            for card in cards:
                correction = normalize_bootstrap_evidence_card(
                    card,
                    selected_source_text=acquired,
                    source_title=candidate.title,
                )
                normalized_cards.append(correction.card)
                if correction.reasons:
                    quality_corrections.append(
                        {
                            "source_candidate_id": candidate.id,
                            "evidence_card_id": card.id,
                            "reasons": list(correction.reasons),
                        }
                    )
            cards = normalized_cards
            mapping = json.loads(existing_nodes_path.read_text(encoding="utf-8"))
            nodes = TypeAdapter(list[BootstrapKnowledgeNodeCandidate]).validate_python(
                mapping.get("nodes", [])
            )
            relationships = TypeAdapter(
                list[BootstrapRelationshipCandidate]
            ).validate_python(mapping.get("relationships", []))
            if candidate.source_category == SourceCategory.PRACTITIONER:
                cards, nodes, relationships = _remove_scientific_channel_leakage(
                    cards, nodes, relationships
                )
            if source_claim_policy:
                cards, nodes, relationships = _filter_cached_artifacts_by_policy(
                    cards,
                    nodes,
                    relationships,
                    source_claim_policy,
                )
            nodes, relationships, support_rejections = (
                _filter_unsupported_entity_nodes(cards, nodes, relationships)
            )
            semantic_rejections = _unique_dicts(
                [*mapping.get("semantic_rejections", []), *support_rejections]
            )
            extracted = json.loads(
                (output / "extracted_claims" / f"{candidate.id}.json").read_text(
                    encoding="utf-8"
                )
            )
            extracted = _synchronize_extracted_claims(extracted, cards)
            previous_pipeline_version = existing_source.get(
                "semantic_pipeline_version"
            )
            existing_source = {
                **existing_source,
                "semantic_pipeline_version": SEMANTIC_PIPELINE_VERSION,
                "claims_generated": len(cards)
                + sum(
                    item.get("claim_role") == "context_only"
                    for item in extracted.get("claims", [])
                ),
                "nodes_generated": len(nodes),
                "relationships_generated": len(relationships),
            }
            if previous_pipeline_version != SEMANTIC_PIPELINE_VERSION:
                existing_source["quality_migrated_from"] = previous_pipeline_version
            _dump(existing_source_path, existing_source)
            _dump(
                existing_cards_path,
                [card.model_dump(mode="json") for card in cards],
            )
            _dump(
                existing_nodes_path,
                {
                    "nodes": [node.model_dump(mode="json") for node in nodes],
                    "relationships": [
                        item.model_dump(mode="json") for item in relationships
                    ],
                    "semantic_rejections": semantic_rejections,
                },
            )
            _dump(
                output / "extracted_claims" / f"{candidate.id}.json",
                extracted,
            )
            return {
                "source": existing_source,
                "later_review": _later_review(existing_source, source_approval),
                "cards": cards,
                "nodes": nodes,
                "relationships": relationships,
                "context_only_claims": sum(
                    item.get("claim_role") == "context_only"
                    for item in extracted.get("claims", [])
                ),
                "semantic_rejections": semantic_rejections,
                "quality_corrections": quality_corrections,
                "error": None,
            }
    source_record = {
        **base_source,
        "source_approval_id": source_approval.id,
        "source_version_id": source_approval.source_version_id,
        "content_hash": source_approval.content_hash,
        "content_access": source_approval.content_access.value,
        "content_characters_used": len(acquired),
        "extraction_scope": source_approval.extraction_scope.model_dump(mode="json"),
        "extraction_status": "attempted",
    }
    _dump(output / "sources" / f"{candidate.id}.json", source_record)
    if request_interval_seconds > 0:
        time.sleep(request_interval_seconds)
    extractor = ResearchKnowledgeExtractor(
        GLMClient(
            glm_api_key,
            glm_model,
            timeout_seconds=glm_timeout_seconds,
            max_retries=glm_max_retries,
        ),
        chunk_size=max_source_characters + 500,
        chunk_overlap=0,
        mode=mode,
    )
    proposal = None
    last_error = None
    for attempt in range(3):
        try:
            proposal = extractor.extract_candidate(enriched, citation, source_approval)
            break
        except Exception as exc:
            last_error = exc
            if "429" not in str(exc) or attempt == 2:
                break
            time.sleep(20 * (attempt + 1))
    if proposal is None:
        assert last_error is not None
        exc = last_error
        error = {
            "candidate_id": candidate.id,
            "title": candidate.title,
            "error_type": type(exc).__name__,
            "error": _safe_error(exc),
        }
        source_record = {**source_record, "extraction_status": "failed"}
        _dump(output / "sources" / f"{candidate.id}.json", source_record)
        _dump(
            output / "extracted_claims" / f"{candidate.id}.json",
            {"source": base_source, "claims": [], "error": error},
        )
        review = _later_review(source_record, source_approval)
        review["claim_review_status"] = "extraction_failed"
        return {
            "source": source_record,
            "later_review": review,
            "cards": [],
            "nodes": [],
            "relationships": [],
            "context_only_claims": 0,
            "semantic_rejections": [],
            "quality_corrections": [],
            "error": error,
        }

    policy_rejections: list[dict[str, str]] = []
    if source_claim_policy:
        proposal, policy_rejections = _apply_source_claim_policy(
            proposal,
            source_claim_policy,
        )
    assembler = BootstrapKnowledgeAssembler()
    nodes, relationships = assembler.assemble(proposal)
    nodes, relationships, support_rejections = _filter_unsupported_entity_nodes(
        proposal.evidence_cards,
        nodes,
        relationships,
    )
    semantic_rejections = [
        *policy_rejections,
        *assembler.rejections,
        *support_rejections,
    ]
    claims = [
        {
            "evidence_card_id": card.id,
            "claim": card.claim,
            "source_locator": card.locator.source_locator,
            "evidence_excerpt": card.evidence_excerpt,
            "evidence_class": card.evidence_class.value,
            "claim_role": card.claim_role.value,
            "knowledge_origin": card.knowledge_origin.value,
            "extraction_confidence": card.extraction_confidence,
            "confidence": card.confidence.model_dump(mode="json")
            if card.confidence
            else None,
            "limitations": card.limitations,
            "contradiction_status": card.contradiction_status.value,
            "review_status": card.review.review_status.value,
        }
        for card in proposal.evidence_cards
    ]
    claims.extend(
        {
            "evidence_card_id": None,
            "claim": claim.statement,
            "claim_role": claim.claim_role.value,
            "source_locator": claim.locator or candidate.url,
            "evidence_excerpt": claim.evidence_excerpt,
            "evidence_class": claim.evidence_class.value,
            "knowledge_origin": None,
            "extraction_confidence": claim.confidence,
            "confidence": None,
            "limitations": claim.limitations,
            "contradiction_status": "not_checked",
            "review_status": "context_only",
        }
        for claim in proposal.context_claims
    )
    _dump(
        output / "extracted_claims" / f"{candidate.id}.json",
        {
            "source_candidate_id": candidate.id,
            "source_version_id": source_approval.source_version_id,
            "claims": claims,
            "extraction_limitations": proposal.limitations,
            "extraction_conflicts": proposal.conflicts,
        },
    )
    _dump(
        output / "evidence_cards" / f"{candidate.id}.json",
        [card.model_dump(mode="json") for card in proposal.evidence_cards],
    )
    _dump(
        output / "knowledge_nodes" / f"{candidate.id}.json",
        {
            "nodes": [node.model_dump(mode="json") for node in nodes],
            "relationships": [item.model_dump(mode="json") for item in relationships],
            "semantic_rejections": semantic_rejections,
        },
    )
    source_record = {
        **source_record,
        "extraction_status": "completed",
        "claims_generated": len(proposal.evidence_cards) + len(proposal.context_claims),
        "nodes_generated": len(nodes),
        "relationships_generated": len(relationships),
    }
    _dump(output / "sources" / f"{candidate.id}.json", source_record)
    review = _later_review(source_record, source_approval)
    if not proposal.evidence_cards:
        review["claim_review_status"] = "bootstrap_generated_empty"
    return {
        "source": source_record,
        "later_review": review,
        "cards": proposal.evidence_cards,
        "nodes": nodes,
        "relationships": relationships,
        "context_only_claims": len(proposal.context_claims),
        "semantic_rejections": semantic_rejections,
        "quality_corrections": [],
        "error": None,
    }


def _load_candidates(source_run: Path) -> list[ResearchCandidate]:
    values = []
    for name in ("academic-candidates.json", "practitioner-candidates.json"):
        values.extend(json.loads((source_run / "candidates" / name).read_text()))
    return TypeAdapter(list[ResearchCandidate]).validate_python(values)


def _load_source_records(source_run: Path) -> list[dict[str, Any]]:
    values = []
    for name in (
        "verified-academic-sources.json",
        "access-checked-practitioner-sources.json",
    ):
        values.extend(json.loads((source_run / "sources" / name).read_text()))
    return values


def _load_source_claim_policies(
    source_runs: list[Path],
) -> dict[str, dict[str, Any]]:
    """Load optional, source-bound claim allowlists emitted by audited adapters."""

    output: dict[str, dict[str, Any]] = {}
    for source_run in source_runs:
        path = source_run / "policies" / "source-claim-policies.json"
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != "bootstrap-source-claim-policy-0.1":
            raise ValueError(f"unsupported source claim policy schema: {path}")
        sources = raw.get("sources")
        if not isinstance(sources, dict):
            raise ValueError(f"invalid source claim policy collection: {path}")
        for candidate_id, policy in sources.items():
            if not isinstance(policy, dict) or policy.get("candidate_id") != candidate_id:
                raise ValueError(f"invalid source claim policy identity: {candidate_id}")
            try:
                roles = [ClaimRole(value) for value in policy["allowed_claim_roles"]]
                origin = KnowledgeOrigin(policy["knowledge_origin"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid source claim policy values: {candidate_id}"
                ) from exc
            if not roles or ClaimRole.CONTEXT_ONLY not in roles:
                raise ValueError(
                    f"source claim policy must preserve context_only: {candidate_id}"
                )
            allowed_for_origin = (
                _PRACTITIONER_POLICY_ROLES
                if origin == KnowledgeOrigin.EXPERT_PRACTICE
                else _ACADEMIC_POLICY_ROLES
                if origin == KnowledgeOrigin.SCIENTIFIC_EVIDENCE
                else frozenset()
            )
            if not allowed_for_origin or not set(roles).issubset(allowed_for_origin):
                raise ValueError(
                    "source claim roles violate their knowledge-origin boundary: "
                    f"{candidate_id}"
                )
            normalized = {
                **policy,
                "allowed_claim_roles": [role.value for role in roles],
                "knowledge_origin": origin.value,
            }
            existing = output.get(candidate_id)
            if existing is not None and existing != normalized:
                raise ValueError(f"conflicting source claim policies: {candidate_id}")
            output[candidate_id] = normalized
    return output


def _verify_prepared_input_manifest(
    source_run: Path,
    output: Path,
) -> dict[str, str] | None:
    """Verify every prepared-input byte before it can reach GLM."""

    path = source_run / "preparation-manifest.json"
    if not path.exists():
        return None
    source_root = source_run.resolve()
    output_root = output.resolve()
    if (
        output_root not in source_root.parents
        or source_root == output_root
        or source_root.relative_to(output_root).parts[0]
        not in {"input", "input_batches"}
    ):
        raise ValueError(
            "prepared bootstrap input must be isolated below output/input[_batches]"
        )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != "bootstrap-generic-input-0.1"
        or manifest.get("run_id") != output.name
        or manifest.get("mode") != "dry_run"
    ):
        raise ValueError("invalid prepared bootstrap input manifest")
    recorded = str(manifest.get("manifest_sha256") or "")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if recorded != _canonical_json_hash(unsigned):
        raise ValueError("prepared bootstrap input manifest hash mismatch")
    source_ledger_sha256 = str(manifest.get("source_ledger_sha256") or "")
    if source_ledger_sha256 in SUPERSEDED_PREPARED_INPUT_LEDGERS:
        raise ValueError("superseded prepared bootstrap input ledger cannot execute")

    allowed_root = source_root
    for item in manifest.get("files", []):
        relative = Path(str(item.get("path") or ""))
        candidate = (output / relative).resolve()
        if allowed_root not in candidate.parents:
            raise ValueError("prepared bootstrap input file escapes input root")
        try:
            data = candidate.read_bytes()
        except OSError as exc:
            raise ValueError("prepared bootstrap input file is missing") from exc
        if hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise ValueError("prepared bootstrap input file hash mismatch")
        if len(data) != item.get("bytes"):
            raise ValueError("prepared bootstrap input file length mismatch")
    constraints = manifest.get("constraints") or {}
    if any(
        constraints.get(key) is not False
        for key in (
            "glm_called",
            "database_modified",
            "approval_created",
            "qdrant_modified",
            "production_collection_modified",
            "human_verified",
        )
    ) or constraints.get("review_status_after_extraction") != "bootstrap_generated":
        raise ValueError("prepared bootstrap input safety constraints are invalid")
    lineage = {
        "source_ledger_sha256": source_ledger_sha256,
        "semantic_audit_sha256": str(manifest["semantic_audit_sha256"]),
        "preparation_manifest_sha256": recorded,
    }
    independent_audit_sha256 = manifest.get("independent_audit_sha256")
    if independent_audit_sha256 is not None:
        lineage["independent_audit_sha256"] = str(independent_audit_sha256)
    return lineage


def _deduplicate_candidates(
    candidates,
) -> list[ResearchCandidate]:
    """Preserve discovery order while deduplicating DOI, then URL, then title."""

    seen: set[str] = set()
    values: list[ResearchCandidate] = []
    for candidate in candidates:
        identity = (
            f"doi:{candidate.doi.casefold()}"
            if candidate.doi
            else f"url:{candidate.url.strip().casefold()}"
            if candidate.url.strip()
            else f"title:{_normalize_title(candidate.title)}"
        )
        if identity in seen:
            continue
        seen.add(identity)
        values.append(candidate)
    return values


def _deduplicate_source_records(records) -> list[dict[str, Any]]:
    seen: set[str] = set()
    values: list[dict[str, Any]] = []
    for record in records:
        candidate_id = str(record["candidate_id"])
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        values.append(record)
    return values


def _remove_scientific_channel_leakage(
    cards: list[EvidenceCard],
    nodes: list[BootstrapKnowledgeNodeCandidate],
    relationships: list[BootstrapRelationshipCandidate],
) -> tuple[
    list[EvidenceCard],
    list[BootstrapKnowledgeNodeCandidate],
    list[BootstrapRelationshipCandidate],
]:
    """Fail closed when cached practitioner claims claim scientific status."""

    retained_cards = [
        card
        for card in cards
        if card.knowledge_origin != KnowledgeOrigin.SCIENTIFIC_EVIDENCE
    ]
    retained_ids = {card.id for card in retained_cards}

    retained_nodes = []
    retained_entity_ids = set()
    for node in nodes:
        supporting = [
            card_id
            for card_id in node.supporting_evidence_ids
            if card_id in retained_ids
        ]
        if not supporting:
            continue
        rebuilt = BootstrapKnowledgeNodeCandidate(
            **node.model_dump(exclude={"id", "supporting_evidence_ids"}),
            supporting_evidence_ids=supporting,
        )
        retained_nodes.append(rebuilt)
        retained_entity_ids.add(rebuilt.entity.id)

    retained_relationships = []
    for relationship in relationships:
        supporting = [
            card_id
            for card_id in relationship.supporting_evidence_ids
            if card_id in retained_ids
        ]
        if (
            not supporting
            or relationship.relationship.source_id not in retained_entity_ids
            or relationship.relationship.target_id not in retained_entity_ids
        ):
            continue
        retained_relationships.append(
            BootstrapRelationshipCandidate(
                **relationship.model_dump(
                    exclude={"id", "supporting_evidence_ids"}
                ),
                supporting_evidence_ids=supporting,
            )
        )
    return retained_cards, retained_nodes, retained_relationships


def _apply_source_claim_policy(
    proposal: KnowledgeProposal,
    policy: dict[str, Any],
) -> tuple[KnowledgeProposal, list[dict[str, str]]]:
    """Fail closed on claim roles/origins outside a strict source audit."""

    allowed_roles = {ClaimRole(value) for value in policy["allowed_claim_roles"]}
    expected_origin = KnowledgeOrigin(policy["knowledge_origin"])
    retained_cards = [
        card
        for card in proposal.evidence_cards
        if card.claim_role in allowed_roles
        and card.knowledge_origin == expected_origin
    ]
    retained_ids = {card.id for card in retained_cards}
    retained_context = [
        claim
        for claim in proposal.context_claims
        if claim.claim_role in allowed_roles
    ]
    entity_proposals = []
    for item in proposal.entity_proposals:
        supporting = [
            card_id
            for card_id in item.supporting_evidence_card_ids
            if card_id in retained_ids
        ]
        if supporting:
            entity_proposals.append(
                item.model_copy(update={"supporting_evidence_card_ids": supporting})
            )
    relationship_proposals = []
    for item in proposal.relationship_proposals:
        supporting = [
            card_id
            for card_id in item.supporting_evidence_card_ids
            if card_id in retained_ids
        ]
        if supporting:
            relationship_proposals.append(
                item.model_copy(update={"supporting_evidence_card_ids": supporting})
            )

    rejections: list[dict[str, str]] = []
    for card in proposal.evidence_cards:
        if card.id in retained_ids:
            continue
        reason = (
            f"claim_role {card.claim_role.value} is outside strict Source audit"
            if card.claim_role not in allowed_roles
            else (
                f"knowledge_origin {card.knowledge_origin.value} does not match "
                f"audited {expected_origin.value}"
            )
        )
        rejections.append(
            {
                "mapping_id": card.id,
                "relation_type": "claim_policy",
                "source_entity": proposal.candidate.title,
                "target_entity": card.claim[:160],
                "reason": reason,
            }
        )
    for claim in proposal.context_claims:
        if claim not in retained_context:
            rejections.append(
                {
                    "mapping_id": "context_only",
                    "relation_type": "claim_policy",
                    "source_entity": proposal.candidate.title,
                    "target_entity": claim.statement[:160],
                    "reason": (
                        f"claim_role {claim.claim_role.value} is outside strict Source audit"
                    ),
                }
            )
    filtered = KnowledgeProposal(
        **proposal.model_dump(
            exclude={
                "id",
                "context_claims",
                "evidence_cards",
                "entity_proposals",
                "relationship_proposals",
                "limitations",
            }
        ),
        context_claims=retained_context,
        evidence_cards=retained_cards,
        entity_proposals=entity_proposals,
        relationship_proposals=relationship_proposals,
        limitations=[
            *proposal.limitations,
            *(
                [
                    f"Strict Source audit discarded {len(rejections)} proposal(s) "
                    "outside the allowed epistemic channel."
                ]
                if rejections
                else []
            ),
        ],
    )
    return filtered, rejections


def _filter_cached_artifacts_by_policy(
    cards: list[EvidenceCard],
    nodes: list[BootstrapKnowledgeNodeCandidate],
    relationships: list[BootstrapRelationshipCandidate],
    policy: dict[str, Any],
) -> tuple[
    list[EvidenceCard],
    list[BootstrapKnowledgeNodeCandidate],
    list[BootstrapRelationshipCandidate],
]:
    allowed_roles = {ClaimRole(value) for value in policy["allowed_claim_roles"]}
    expected_origin = KnowledgeOrigin(policy["knowledge_origin"])
    retained_cards = [
        card
        for card in cards
        if card.claim_role in allowed_roles
        and card.knowledge_origin == expected_origin
    ]
    retained_ids = {card.id for card in retained_cards}
    retained_nodes: list[BootstrapKnowledgeNodeCandidate] = []
    retained_entity_ids: set[str] = set()
    for node in nodes:
        supporting = [
            card_id
            for card_id in node.supporting_evidence_ids
            if card_id in retained_ids
        ]
        if not supporting or node.knowledge_origin != expected_origin:
            continue
        rebuilt = BootstrapKnowledgeNodeCandidate(
            **node.model_dump(exclude={"id", "supporting_evidence_ids"}),
            supporting_evidence_ids=supporting,
        )
        retained_nodes.append(rebuilt)
        retained_entity_ids.add(rebuilt.entity.id)
    retained_relationships: list[BootstrapRelationshipCandidate] = []
    for relationship in relationships:
        supporting = [
            card_id
            for card_id in relationship.supporting_evidence_ids
            if card_id in retained_ids
        ]
        if (
            not supporting
            or relationship.knowledge_origin != expected_origin
            or relationship.relationship.source_id not in retained_entity_ids
            or relationship.relationship.target_id not in retained_entity_ids
        ):
            continue
        retained_relationships.append(
            BootstrapRelationshipCandidate(
                **relationship.model_dump(
                    exclude={"id", "supporting_evidence_ids"}
                ),
                supporting_evidence_ids=supporting,
            )
        )
    return retained_cards, retained_nodes, retained_relationships


def _filter_unsupported_entity_nodes(
    cards: list[EvidenceCard],
    nodes: list[BootstrapKnowledgeNodeCandidate],
    relationships: list[BootstrapRelationshipCandidate],
) -> tuple[
    list[BootstrapKnowledgeNodeCandidate],
    list[BootstrapRelationshipCandidate],
    list[dict[str, str]],
]:
    """Reject named constructs that are absent from their supporting cards.

    Topic tags and a fluent GLM definition are not evidence.  Constructs that
    audits have identified as especially prone to inferred linking must appear
    in the retained claim or excerpt before a Knowledge Node can be projected.
    """

    cards_by_id = {card.id: card for card in cards}
    explicit_constructs = {"working memory"}
    retained_nodes: list[BootstrapKnowledgeNodeCandidate] = []
    retained_entity_ids: set[str] = set()
    rejections: list[dict[str, str]] = []
    for node in nodes:
        name = " ".join(node.entity.name.casefold().split())
        supporting_cards = [
            cards_by_id[card_id]
            for card_id in node.supporting_evidence_ids
            if card_id in cards_by_id
        ]
        evidence_text = " ".join(
            f"{card.claim} {card.evidence_excerpt}" for card in supporting_cards
        ).casefold()
        if name in explicit_constructs and name not in evidence_text:
            rejections.append(
                {
                    "mapping_id": node.id,
                    "relation_type": "entity_support",
                    "source_entity": node.entity.name,
                    "target_entity": ",".join(node.supporting_evidence_ids),
                    "reason": (
                        "named cognitive construct is absent from the retained "
                        "supporting Evidence Card claim and excerpt"
                    ),
                }
            )
            continue
        retained_nodes.append(node)
        retained_entity_ids.add(node.entity.id)

    retained_relationships = [
        item
        for item in relationships
        if item.relationship.source_id in retained_entity_ids
        and item.relationship.target_id in retained_entity_ids
    ]
    return retained_nodes, retained_relationships, rejections


def _synchronize_extracted_claims(
    extracted: dict[str, Any],
    cards: list[EvidenceCard],
) -> dict[str, Any]:
    """Keep the human-review claim ledger aligned with corrected card payloads."""

    card_by_id = {card.id: card for card in cards}
    synchronized = []
    for item in extracted.get("claims", []):
        value = dict(item)
        card = card_by_id.get(str(value.get("evidence_card_id") or ""))
        if card is not None:
            value["evidence_class"] = card.evidence_class.value
            value["knowledge_origin"] = card.knowledge_origin.value
            value["source_locator"] = card.locator.source_locator
        synchronized.append(value)
    return {**extracted, "claims": synchronized}


def _unique_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    seen = set()
    for value in values:
        identity = _canonical_json_hash(value)
        if identity not in seen:
            seen.add(identity)
            output.append(value)
    return output


def _canonical_json_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_content(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if "sources" in raw:
        return {
            url: str(item.get("content") or "")
            for url, item in raw["sources"].items()
        }
    if "results" in raw:
        return {
            str(item["url"]): str(item.get("raw_content") or item.get("content") or "")
            for item in raw["results"]
            if item.get("url")
        }
    raise ValueError(f"unsupported content bundle shape: {path}")


def _content_for(candidate: ResearchCandidate, content: dict[str, str]) -> str:
    if candidate.url in content:
        return content[candidate.url]
    if candidate.doi:
        doi = candidate.doi.casefold()
        for value in content.values():
            if doi in value.casefold():
                return value
    normalized_title = _normalize_title(candidate.title)
    for value in content.values():
        if normalized_title and normalized_title in _normalize_title(value[:500]):
            return value
    return ""


def _prepare_source_content(
    content: str,
    candidate: ResearchCandidate,
    *,
    max_characters: int,
) -> str:
    """Select traceable content sections instead of truncating navigation headers."""

    cleaned = content.replace("\r\n", "\n").strip()
    if len(cleaned) <= max_characters:
        return cleaned
    if candidate.source_category == SourceCategory.ACADEMIC:
        selected = _academic_sections(cleaned, max_characters)
        if selected:
            return selected

    anchors = _title_anchors(candidate.title)
    lowered = cleaned.casefold()
    positions = [lowered.find(anchor) for anchor in anchors if anchor]
    positions = [position for position in positions if position >= 0]
    if not positions:
        for marker in ("transcript", "interview", "article", "essay"):
            position = lowered.find(marker)
            if position >= 0:
                positions.append(position)
    start = min(positions) if positions else 0
    return cleaned[start : start + max_characters]


def _academic_sections(content: str, max_characters: int) -> str:
    headings = list(
        re.finditer(
            r"(?im)^#{1,4}\s*(abstract|summary|methods?|results?|discussion|conclusions?)\b[^\n]*",
            content,
        )
    )
    if not headings:
        lowered = content.casefold()
        positions = [
            lowered.find(marker)
            for marker in ("abstract", "summary", "introduction")
            if lowered.find(marker) >= 0
        ]
        start = min(positions) if positions else 0
        return content[start : start + max_characters]

    priority = {
        "abstract": 0,
        "summary": 0,
        "result": 1,
        "results": 1,
        "discussion": 2,
        "conclusion": 3,
        "conclusions": 3,
        "method": 4,
        "methods": 4,
    }
    sections = []
    for index, heading in enumerate(headings):
        label = heading.group(1).casefold()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        sections.append((priority.get(label, 9), heading.start(), content[heading.start() : end]))
    selected = []
    remaining = max_characters
    for _, start, section in sorted(sections, key=lambda item: (item[0], item[1])):
        if remaining < 300:
            break
        allowance = min(2_600, remaining)
        excerpt = section[:allowance].strip()
        if not excerpt:
            continue
        selected.append(f"[Source offset {start}]\n{excerpt}")
        remaining -= len(selected[-1]) + 2
    return "\n\n".join(selected)[:max_characters]


def _title_anchors(title: str) -> list[str]:
    normalized = " ".join(title.casefold().split())
    values = [normalized]
    for separator in (" | ", " - ", ": "):
        if separator in normalized:
            values.append(normalized.split(separator, 1)[0].strip())
    return sorted(set(values), key=len, reverse=True)


def _normalize_title(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _later_review(record: dict[str, Any], source_approval) -> dict[str, Any]:
    return {
        "review_item_id": (
            source_review_item_id(source_approval) if source_approval else None
        ),
        "candidate_id": record["candidate_id"],
        "title": record["title"],
        "source_category": record["source_category"],
        "source_review_status": "bootstrap_pending_human_review",
        "claim_review_status": "bootstrap_generated"
        if source_approval
        else "not_extracted",
        "storage_authorization": "not_human_authorized",
        "human_verified": False,
        "required_action": (
            "Review exact content hash, provenance, claims, contradictions, sensitivity, and storage eligibility."
            if source_approval
            else "Acquire readable exact source content before claim review."
        ),
    }


def _safe_error(exc: Exception) -> str:
    value = str(exc)
    value = re.sub(r"[A-Za-z0-9]{20,}\.[A-Za-z0-9_-]{10,}", "[REDACTED]", value)
    return value[:2_000]


def _dump(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _render_report(report: dict[str, Any]) -> str:
    return f"""# MagicForge {report['run_id']}

Mode: `bootstrap`

All artifacts in this run are machine-generated, unverified, and queued for later human review. Production governance remains enabled when `MAGICFORGE_MODE=production`.

## Counts

- Sources processed: {report['sources_processed']}
- Existing discovery sources: {report['source_breakdown']['existing_discovery_sources']}
- Additional existing Bootstrap 001 sources: {report['source_breakdown']['additional_existing_sources']}
- Sources with readable content: {report['sources_with_readable_content']}
- Sources extracted successfully: {report['sources_extracted_successfully']}
- Claims generated: {report['claims_generated']}
- Context-only claims retained without Evidence Cards: {report['context_only_claims_generated']}
- Evidence Cards generated: {report['evidence_cards_generated']}
- Knowledge Nodes generated: {report['knowledge_nodes_generated']}
- Relationship candidates generated: {report['relationships_generated']}
- Semantic proposals rejected by deterministic gates: {report['semantic_rejections']}
- Node proposals before canonical merge: {report['canonical_merge']['node_proposals_before']}
- Relationship proposals before canonical merge: {report['canonical_merge']['relationship_proposals_before']}
- Qdrant points prepared: {report['qdrant_points_prepared']}
- Qdrant points created: {report['qdrant_points_created']}
- Sources requiring later human review: {report['sources_requiring_later_human_review']}
- Extraction errors: {report['extraction_errors']}

## Isolation and safety

- Bootstrap collection: `{BOOTSTRAP_COLLECTION_NAME}`
- Production collection touched: `false`
- Evidence Card review state: `bootstrap_generated`
- Knowledge Node verification state: `unverified`
- Human verified: `false`
- Source attribution, locators, confidence, limitations, contradiction state, provenance, and sensitivity labels are retained.

## Qdrant status

{_qdrant_status(report)}

## Research gaps

{chr(10).join(f'- {item}' for item in report['research_gaps'])}

## Human review required

- Sources: {report['human_review_items']['sources']}
- Evidence Cards: {report['human_review_items']['evidence_cards']}
- Knowledge Nodes: {report['human_review_items']['knowledge_nodes']}
- Relationships: {report['human_review_items']['relationships']}
- Contradiction checks pending: {report['human_review_items']['contradiction_checks_pending']}
"""


def _qdrant_status(report: dict[str, Any]) -> str:
    if not report["qdrant_ingestion_attempted"]:
        return "Bootstrap manifest prepared; Qdrant ingestion was intentionally skipped."
    if report["qdrant_error"]:
        return json.dumps(report["qdrant_error"], ensure_ascii=False)
    if report["qdrant_ingestion_succeeded"]:
        return "Bootstrap manifest was written to Qdrant and verified."
    return "Qdrant ingestion did not produce a receipt."


if __name__ == "__main__":
    main()
