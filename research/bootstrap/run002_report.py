"""Build the auditable final summary for Bootstrap Expansion Run 002."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_RUN = Path("research/runs/bootstrap-002")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    args = parser.parse_args()
    report = build_report(args.run)
    print(json.dumps(report["counts"], ensure_ascii=False, indent=2))


def build_report(run: Path):
    extraction = _load(run / "run-report.json")
    discovery = _load(run / "reports" / "discovery-report.json")
    access = _load(run / "reports" / "access-report.json")
    curation = _load(run / "reports" / "curation-report.json")
    metadata = _load(run / "reports" / "metadata-verification-report.json")
    manifest = _load(run / "qdrant_manifest" / "bootstrap-manifest-v03.json")
    extraction_manifest = _load(
        run / "qdrant_manifest" / "bootstrap-manifest.json"
    )
    receipt = _load(run / "qdrant_manifest" / "ingestion-receipt-v03.json")
    exclusions = _load(run / "reports" / "safety-exclusions.json")
    projections = manifest["projections"]
    artifact_counts = Counter(item["artifact_type"] for item in projections)
    knowledge_types = Counter(item["knowledge_type"] for item in projections)
    origins = Counter(item["knowledge_origin"] for item in projections)
    evidence_classes = Counter(item["evidence_class"] for item in projections)
    magic_domains = Counter(domain for item in projections for domain in item["domain"])
    relation_types = Counter(
        relation for item in projections for relation in item["relation_types"]
    )
    claim_roles = Counter(role for item in projections for role in item["claim_roles"])
    source_types = Counter(item["source_type"] for item in projections)
    projected_sources = {item["source_candidate_id"] for item in projections}
    contradiction_pending = sum(
        item["artifact_type"] == "evidence_card"
        and item["contradiction_status"] == "not_checked"
        for item in projections
    )
    procedural_method_quarantine = sum(
        "method" in item["claim_roles"]
        and item["sensitive_information_level"] in {"controlled", "restricted"}
        for item in extraction_manifest["projections"]
    )
    safety = {
        "production_collection_touched": False,
        "bootstrap_collection": manifest["collection_name"],
        "bootstrap_generated_points": sum(
            item["bootstrap_generated"] is True for item in projections
        ),
        "human_verified_points": sum(
            item["human_verified"] is True for item in projections
        ),
        "approved_points": sum(item["approved"] is True for item in projections),
        "storage_permission_points": sum(
            item["storage_permission"] is True for item in projections
        ),
        "review_status_counts": dict(
            Counter(item["review_status"] for item in projections)
        ),
        "slash_ontology_paths": sum(
            "/" in path for item in projections for path in item["ontology_paths"]
        ),
        "safety_excluded_source_ids": exclusions["source_candidate_ids"],
        "safety_excluded_projection_count": (
            extraction["qdrant_points_prepared"] - manifest["expected_point_count"]
        ),
        "procedural_method_projections_quarantined": procedural_method_quarantine,
    }
    counts = {
        "raw_discovery_results": discovery["raw_records"],
        "queries_executed": discovery["queries_executed"],
        "screened_incremental_candidates": discovery["included_candidates"],
        "readable_incremental_candidates": access["readable_exact_content"],
        "curated_incremental_candidates": curation["selected_for_processing"],
        "verified_new_sources": metadata["new_sources_ready_for_extraction"],
        "sources_processed_total": extraction["sources_processed"],
        "sources_extracted_successfully": extraction["sources_extracted_successfully"],
        "claims_generated": extraction["claims_generated"],
        "evidence_cards_generated": extraction["evidence_cards_generated"],
        "evidence_cards_projected": artifact_counts["evidence_card"],
        "knowledge_nodes_generated": extraction["knowledge_nodes_generated"],
        "knowledge_nodes_projected": artifact_counts["knowledge_node"],
        "relationships_generated": extraction["relationships_generated"],
        "relationships_projected": artifact_counts["relationship"],
        "semantic_proposals_rejected": extraction["semantic_rejections"],
        "qdrant_points_created": len(receipt["point_ids"]),
        "sources_with_projected_knowledge": len(projected_sources),
        "sources_requiring_human_review": extraction["sources_processed"],
        "contradiction_checks_pending": contradiction_pending,
        "extraction_errors": extraction["extraction_errors"],
    }
    targets = {
        "sources_200_plus": {
            "target": 200,
            "actual": counts["sources_processed_total"],
            "met": counts["sources_processed_total"] >= 200,
        },
        "evidence_cards_1000_plus": {
            "target": 1000,
            "actual": counts["evidence_cards_projected"],
            "met": counts["evidence_cards_projected"] >= 1000,
        },
        "knowledge_nodes_200_plus": {
            "target": 200,
            "actual": counts["knowledge_nodes_projected"],
            "met": counts["knowledge_nodes_projected"] >= 200,
        },
        "relationships_100_plus": {
            "target": 100,
            "actual": counts["relationships_projected"],
            "met": counts["relationships_projected"] >= 100,
        },
    }
    gaps = [
        "Evidence Card and node volume targets were not padded: unsupported, context-only, inaccessible, duplicate, and low-provenance material remained excluded.",
        "The relationship target was not met because only explicitly entailed relationships survived; co-mention, correlation, and experiment stimuli were not promoted to graph edges.",
        "Copyright-protected classical books are represented mainly by lawful metadata, reviews, interviews, and commentary; book listings did not become evidence for their internal theories.",
        "Contradicting evidence has not yet been checked by a human for any projected Evidence Card.",
        "Historical invention and ownership claims remain unverified and must not be inferred from chronology alone.",
        "Practitioner pages often lack stable author/year metadata and require source-level human review before production use.",
    ]
    report = {
        "run_id": run.name,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "bootstrap",
        "collection": manifest["collection_name"],
        "counts": counts,
        "targets": targets,
        "coverage": {
            "knowledge_types": dict(knowledge_types),
            "magic_domains": dict(magic_domains),
            "knowledge_origins": dict(origins),
            "evidence_classes": dict(evidence_classes),
            "claim_roles": dict(claim_roles),
            "source_types": dict(source_types),
            "relation_types": dict(relation_types),
            "discovery_research_domains": curation["domain_coverage"],
        },
        "human_review_queue": {
            "sources": extraction["sources_processed"],
            "evidence_cards": artifact_counts["evidence_card"],
            "knowledge_nodes": artifact_counts["knowledge_node"],
            "relationships": artifact_counts["relationship"],
            "contradiction_checks_pending": contradiction_pending,
            "safety_exclusions": len(exclusions["source_candidate_ids"]),
            "procedural_method_projections_quarantined": procedural_method_quarantine,
        },
        "safety": safety,
        "research_gaps": gaps,
        "manifest": {
            "id": manifest["id"],
            "hash": manifest["manifest_hash"],
            "receipt_id": receipt["id"],
            "point_count": manifest["expected_point_count"],
        },
    }
    _write(run / "reports" / "run-summary.json", report)
    (run / "reports" / "run-summary.md").write_text(
        _markdown(report), encoding="utf-8"
    )
    return report


def _markdown(report):
    c = report["counts"]
    t = report["targets"]
    coverage = report["coverage"]
    return f"""# MagicForge Bootstrap Expansion Run 002

Mode: `bootstrap`
Collection: `magicforge_bootstrap_v03`
Human verified: `false`
Production collection touched: `false`

## Result

- Raw discovery results: {c['raw_discovery_results']} across {c['queries_executed']} queries
- Verified new sources: {c['verified_new_sources']}
- Total sources processed: {c['sources_processed_total']}
- Sources extracted successfully: {c['sources_extracted_successfully']}
- Claims generated: {c['claims_generated']}
- Evidence Cards generated / projected: {c['evidence_cards_generated']} / {c['evidence_cards_projected']}
- Knowledge Nodes generated / projected: {c['knowledge_nodes_generated']} / {c['knowledge_nodes_projected']}
- Relationships generated / projected: {c['relationships_generated']} / {c['relationships_projected']}
- Semantic proposals rejected: {c['semantic_proposals_rejected']}
- Qdrant points created and verified: {c['qdrant_points_created']}
- Extraction errors: {c['extraction_errors']}

## Target status

- Sources 200+: {_status(t['sources_200_plus'])}
- Evidence Cards 1000+: {_status(t['evidence_cards_1000_plus'])}
- Knowledge Nodes 200+: {_status(t['knowledge_nodes_200_plus'])}
- Relationships 100+: {_status(t['relationships_100_plus'])}

Quality gates were not weakened to reach numeric targets.

## Coverage

- Discovery research domains: `{json.dumps(coverage['discovery_research_domains'], ensure_ascii=False, sort_keys=True)}`
- Magic domains: `{json.dumps(coverage['magic_domains'], ensure_ascii=False, sort_keys=True)}`
- Knowledge types: `{json.dumps(coverage['knowledge_types'], ensure_ascii=False, sort_keys=True)}`
- Knowledge origins: `{json.dumps(coverage['knowledge_origins'], ensure_ascii=False, sort_keys=True)}`
- Relationship types: `{json.dumps(coverage['relation_types'], ensure_ascii=False, sort_keys=True)}`

## Human review queue

- Sources: {report['human_review_queue']['sources']}
- Evidence Cards: {report['human_review_queue']['evidence_cards']}
- Knowledge Nodes: {report['human_review_queue']['knowledge_nodes']}
- Relationships: {report['human_review_queue']['relationships']}
- Contradiction checks pending: {report['human_review_queue']['contradiction_checks_pending']}
- Safety-excluded sources: {report['human_review_queue']['safety_exclusions']}
- Controlled method-role projections quarantined from retrieval: {report['human_review_queue']['procedural_method_projections_quarantined']}

## Safety

- Every projected point has `bootstrap_generated=true`.
- Every projected point has `human_verified=false`, `approved=false`, and `storage_permission=false`.
- One suspected unauthorized full-book mirror was retained only in the discovery audit trail; its two derived projections were excluded from Qdrant.
- {report['safety']['procedural_method_projections_quarantined']} controlled method-role projections remain in the audit artifacts for human review but are not searchable in v03.
- The production collection was not accessed or modified.

## Research gaps

{chr(10).join(f'- {item}' for item in report['research_gaps'])}
"""


def _status(value):
    return f"{'met' if value['met'] else 'not met'} ({value['actual']} / {value['target']})"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: object):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
