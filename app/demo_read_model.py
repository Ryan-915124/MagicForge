"""Read-only application projections for the isolated synthetic Demo corpus."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.knowledge_read_model import (
    CorpusStatistics,
    KnowledgeReadModelError,
    KnowledgeSearchPage,
    ProjectionSummary,
)
from app.research_console_read_model import (
    PipelineStageObservation,
    ResearchConsoleSnapshot,
    RunHistoryObservation,
)
from app.runtime_corpus import ActiveCorpus
from knowledge.bootstrap import (
    BootstrapKnowledgeNodeCandidate,
    BootstrapRelationshipCandidate,
)
from knowledge.demo import DemoCorpusBundle
from knowledge.evidence import EvidenceCard
from knowledge.governance import MagicForgeMode, SensitiveInformationLevel
from retrieval.interfaces import (
    KnowledgeSearchFilter,
    RetrievalAuthorization,
    SearchResult,
    require_retrieval_authorization,
)


class DemoKnowledgeReadModel:
    """Expose only objects materialized from ``data/demo/corpus.json``."""

    def __init__(self, active_corpus: ActiveCorpus, bundle: DemoCorpusBundle) -> None:
        if active_corpus.mode != MagicForgeMode.BOOTSTRAP:
            raise KnowledgeReadModelError("the Demo reader requires Bootstrap isolation")
        self.active_corpus = active_corpus
        self._bundle = bundle
        self._payloads = {
            str(payload["artifact_id"]): payload for payload in bundle.payloads()
        }
        self._node_by_identifier = {
            identifier: item
            for item in bundle.nodes
            for identifier in (item.id, item.entity.id)
        }
        self._evidence_by_id = {item.id: item for item in bundle.evidence_cards}
        self._summary = ProjectionSummary(
            run_id=active_corpus.corpus_id,
            collection=active_corpus.collection_name,
            manifest_id=active_corpus.manifest_id,
            generated_at=_iso(bundle.spec.generated_at),
            sources=len(bundle.sources),
            knowledge_nodes=len(bundle.nodes),
            relationships=len(bundle.relationships),
            renderable_relationships=len(bundle.relationships),
            evidence_cards=len(bundle.evidence_cards),
            qdrant_points=len(bundle.projections),
            bootstrap_generated=True,
            human_verified=False,
        )
        artifact_types = Counter(item.artifact_type.value for item in bundle.projections)
        domain_memberships: Counter[str] = Counter()
        knowledge_origins: Counter[str] = Counter()
        knowledge_types: Counter[str] = Counter()
        for projection in bundle.projections:
            domain_memberships.update(value.value for value in projection.domain)
            knowledge_origins[projection.knowledge_origin.value] += 1
            knowledge_types[projection.knowledge_type.value] += 1
        source_categories = Counter(item.source_category for item in bundle.sources)
        self._statistics = CorpusStatistics(
            sources_with_projected_knowledge=len(bundle.sources),
            source_categories=dict(sorted(source_categories.items())),
            artifact_types=dict(sorted(artifact_types.items())),
            domain_memberships=dict(sorted(domain_memberships.items())),
            knowledge_origins=dict(sorted(knowledge_origins.items())),
            knowledge_types=dict(sorted(knowledge_types.items())),
            human_verified_points=0,
            contradiction_checks_pending=len(bundle.evidence_cards),
            pending_human_review_sources=len(bundle.sources),
            procedural_method_projections_quarantined=0,
            production_collection_touched=False,
        )

    def validate(self) -> None:
        expected = self.active_corpus
        if (
            expected.corpus_id != self._bundle.spec.corpus_id
            or expected.collection_name != self._bundle.spec.collection_name
            or expected.manifest_id != self._bundle.manifest_id
            or expected.manifest_hash != self._bundle.manifest_hash
            or expected.expected_point_ids != self._bundle.expected_point_ids
            or dict(expected.expected_payload_checksums)
            != self._bundle.payload_checksums
        ):
            raise KnowledgeReadModelError(
                "the Demo read model does not match the active corpus"
            )

    @property
    def summary(self) -> ProjectionSummary:
        return self._summary

    @property
    def statistics(self) -> CorpusStatistics:
        return self._statistics

    def search(
        self,
        query: str,
        limit: int,
        filters: KnowledgeSearchFilter,
        *,
        authorization: RetrievalAuthorization | None = None,
    ) -> KnowledgeSearchPage:
        auth = require_retrieval_authorization(authorization)
        include_evidence = (
            not filters.knowledge_types or "evidence" in filters.knowledge_types
        )
        include_graph = not filters.knowledge_types or any(
            value != "evidence" for value in filters.knowledge_types
        )
        evidence = (
            self._select_evidence(query, limit, filters, auth)
            if include_evidence
            else []
        )
        nodes, relationships = (
            self._select_graph(query, limit, filters, auth)
            if include_graph
            else ([], [])
        )
        results: list[SearchResult] = []
        for item in evidence:
            payload = self._payloads[item.id]
            results.append(
                SearchResult(
                    text=str(payload.get("text") or item.claim),
                    score=_lexical_score(query, _evidence_text(item)),
                    payload=payload,
                )
            )
        for item in nodes:
            payload = self._payloads[item.id]
            results.append(
                SearchResult(
                    text=str(payload.get("text") or item.definition),
                    score=_lexical_score(query, _node_text(item)),
                    payload=payload,
                )
            )
        results.sort(key=lambda item: item.score, reverse=True)
        return KnowledgeSearchPage(
            results=results[:limit],
            evidence_cards=evidence,
            nodes=nodes,
            relationships=[item.relationship for item in relationships],
            projection=self.summary,
        )

    def get_node(
        self,
        identifier: str,
        *,
        authorization: RetrievalAuthorization | None = None,
    ) -> BootstrapKnowledgeNodeCandidate | None:
        auth = require_retrieval_authorization(authorization)
        item = self._node_by_identifier.get(identifier)
        if item is None or not _authorized(self._payloads.get(item.id), auth):
            return None
        return item

    def get_evidence(
        self,
        identifier: str,
        *,
        authorization: RetrievalAuthorization | None = None,
    ) -> EvidenceCard | None:
        auth = require_retrieval_authorization(authorization)
        item = self._evidence_by_id.get(identifier)
        if item is None or not _authorized(self._payloads.get(item.id), auth):
            return None
        return item

    def _select_evidence(
        self,
        query: str,
        limit: int,
        filters: KnowledgeSearchFilter,
        auth: RetrievalAuthorization,
    ) -> list[EvidenceCard]:
        candidates = [
            item
            for item in self._bundle.evidence_cards
            if _authorized(self._payloads.get(item.id), auth)
            and _common_filters(
                origin=item.knowledge_origin.value,
                domains=[value.value for value in item.applicable_domain],
                evidence_level=item.evidence_level.value,
                ontology_paths=item.ontology_paths,
                filters=filters,
            )
        ]
        return _rank(query, candidates, _evidence_text, limit)

    def _select_graph(
        self,
        query: str,
        limit: int,
        filters: KnowledgeSearchFilter,
        auth: RetrievalAuthorization,
    ) -> tuple[
        list[BootstrapKnowledgeNodeCandidate],
        list[BootstrapRelationshipCandidate],
    ]:
        candidates = [
            item
            for item in self._bundle.nodes
            if _authorized(self._payloads.get(item.id), auth)
            and _common_filters(
                origin=item.knowledge_origin.value,
                domains=[value.value for value in item.domains],
                evidence_level=str(self._payloads[item.id].get("evidence_level") or ""),
                ontology_paths=item.ontology_paths,
                filters=filters,
            )
            and (
                not filters.knowledge_types
                or str(self._payloads[item.id].get("knowledge_type") or "")
                in filters.knowledge_types
            )
            and (not filters.entity_types or item.entity.type.value in filters.entity_types)
            and (not filters.entity_ids or item.entity.id in filters.entity_ids)
        ]
        relationships = [
            item
            for item in self._bundle.relationships
            if _authorized(self._payloads.get(item.id), auth)
            and (
                not filters.relation_types
                or item.relationship.type.value in filters.relation_types
            )
        ]
        selected = _rank(query, candidates, _node_text, limit)
        if query.strip():
            candidate_by_entity_id = {item.entity.id: item for item in candidates}
            matched_ids = {item.entity.id for item in selected}
            for relationship in relationships:
                endpoints = {
                    relationship.relationship.source_id,
                    relationship.relationship.target_id,
                }
                if not matched_ids & endpoints:
                    continue
                for endpoint in endpoints:
                    neighbor = candidate_by_entity_id.get(endpoint)
                    if neighbor is not None and neighbor not in selected and len(selected) < limit:
                        selected.append(neighbor)
        else:
            selected = candidates[:limit]
        selected_ids = {item.entity.id for item in selected}
        selected_relationships = [
            item
            for item in relationships
            if item.relationship.source_id in selected_ids
            and item.relationship.target_id in selected_ids
        ]
        return selected, selected_relationships


class DemoResearchConsoleReadModel:
    """Construct the console snapshot without reading any research run path."""

    def __init__(self, active_corpus: ActiveCorpus, bundle: DemoCorpusBundle) -> None:
        self.active_corpus = active_corpus
        self._bundle = bundle

    def snapshot(self, settings: Settings) -> ResearchConsoleSnapshot:
        del settings
        bundle = self._bundle
        generated_at = _iso(bundle.spec.generated_at)
        points = len(bundle.projections)
        projection_label = (
            "Qdrant projection"
            if self.active_corpus.storage_kind == "remote"
            else "In-memory projection"
        )
        stages = (
            PipelineStageObservation(
                id="source_registration",
                label="Synthetic source registration",
                status="completed",
                metrics={"sources": len(bundle.sources)},
            ),
            PipelineStageObservation(
                id="claim_materialization",
                label="Deterministic claim materialization",
                status="completed",
                metrics={"claims": len(bundle.claims)},
            ),
            PipelineStageObservation(
                id="projection",
                label=projection_label,
                status="receipt_verified",
                metrics={"points": points},
            ),
        )
        history = RunHistoryObservation(
            run_id=bundle.spec.corpus_id,
            generated_at=generated_at,
            mode="bootstrap",
            collection=bundle.spec.collection_name,
            sources=len(bundle.sources),
            extracted_sources=len(bundle.sources),
            claims=len(bundle.claims),
            evidence_cards=len(bundle.evidence_cards),
            knowledge_nodes=len(bundle.nodes),
            relationships=len(bundle.relationships),
            qdrant_points=points,
            extraction_errors=0,
            status="receipt_verified",
            metric_basis="receipt_verified_projections",
        )
        return ResearchConsoleSnapshot(
            observed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            current_run={
                "run_id": bundle.spec.corpus_id,
                "mode": "bootstrap",
                "generated_at": generated_at,
                "collection": bundle.spec.collection_name,
                "status": "receipt_verified",
            },
            runtime={
                "api": {"status": "ok"},
                "intelligence_instrument": {
                    "provider": "Offline deterministic Demo",
                    "model": "deterministic-demo",
                    "configured": True,
                    "connectivity": "not_probed",
                    "structured_extraction": False,
                },
                "retrieval": {
                    "configured": True,
                    "collection": bundle.spec.collection_name,
                    "storage_kind": self.active_corpus.storage_kind,
                    "connectivity": "not_probed",
                },
            },
            pipeline={"status": "receipt_verified", "stages": stages},
            memory_vault={
                "runtime_collection": bundle.spec.collection_name,
                "audited_collection": bundle.spec.collection_name,
                "alignment_status": "aligned",
                "manifest": {
                    "status": "manifest_verified",
                    "id": bundle.manifest_id,
                    "hash": bundle.manifest_hash,
                    "point_count": points,
                },
                "receipt": {
                    "status": "receipt_verified",
                    "id": bundle.receipt_id,
                    "ingested_at": generated_at,
                    "point_count": points,
                },
                "retrieval_smoke": {
                    "status": "report_verified",
                    "tested_at": generated_at,
                    "query_count": 1,
                    "collection_count": points,
                    "all_returned_hits_bootstrap_safe": True,
                },
                "points": {
                    "manifest": points,
                    "receipt": points,
                    "smoke_observed": points,
                },
                "safety": {
                    "bootstrap_generated_points": points,
                    "human_verified_points": 0,
                    "approved_points": 0,
                    "storage_permission_points": 0,
                    "production_collection_touched": False,
                    "production_collection_present_in_smoke": False,
                    "safety_excluded_projection_count": 0,
                },
            },
            governance={
                "mode": "bootstrap",
                "checkpoint_status": "pending_human_review",
                "sources_pending": len(bundle.sources),
                "evidence_cards_pending": len(bundle.evidence_cards),
                "knowledge_nodes_pending": len(bundle.nodes),
                "relationships_pending": len(bundle.relationships),
                "contradiction_checks_pending": len(bundle.evidence_cards),
                "procedural_method_projections_quarantined": 0,
                "human_verified_points": 0,
                "approved_points": 0,
                "storage_permission_points": 0,
            },
            run_history=(history,),
        )


def _authorized(
    payload: dict[str, object] | None,
    authorization: RetrievalAuthorization,
) -> bool:
    if payload is None:
        return False
    try:
        sensitivity = SensitiveInformationLevel(
            str(payload["sensitive_information_level"])
        )
        rank = int(payload["secret_exposure_rank"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        sensitivity in authorization.allowed_sensitive_levels
        and 0 <= rank <= int(authorization.clearance_rank)
    )


def _common_filters(
    *,
    origin: str,
    domains: list[str],
    evidence_level: str,
    ontology_paths: list[str],
    filters: KnowledgeSearchFilter,
) -> bool:
    return (
        (not filters.knowledge_origins or origin in filters.knowledge_origins)
        and (not filters.domains or bool(set(domains) & set(filters.domains)))
        and (not filters.evidence_levels or evidence_level in filters.evidence_levels)
        and (
            not filters.ontology_paths
            or bool(set(ontology_paths) & set(filters.ontology_paths))
        )
    )


def _rank(query: str, values: list[Any], text_fn, limit: int) -> list[Any]:
    if not query.strip():
        return values[:limit]
    scored = [(_lexical_score(query, text_fn(item)), item) for item in values]
    return [
        item
        for score, item in sorted(scored, key=lambda pair: pair[0], reverse=True)
        if score > 0
    ][:limit]


def _lexical_score(query: str, text: str) -> float:
    normalized = " ".join(query.casefold().split())
    if not normalized:
        return 1.0
    terms = re.findall(r"[\w-]+", normalized, flags=re.UNICODE)
    haystack = " ".join(text.casefold().split())
    matched = sum(1 for term in terms if term in haystack)
    if not terms or not matched:
        return 0.0
    return min(
        1.0,
        (matched / len(terms)) * 0.8 + (0.2 if normalized in haystack else 0.0),
    )


def _evidence_text(item: EvidenceCard) -> str:
    return " ".join(
        (
            item.claim,
            item.evidence_excerpt,
            item.magic_application or "",
            " ".join(item.topic_tags),
            " ".join(item.ontology_paths),
        )
    )


def _node_text(item: BootstrapKnowledgeNodeCandidate) -> str:
    return " ".join(
        (
            item.entity.name,
            item.definition,
            item.entity.description or "",
            " ".join(item.topic_tags),
            " ".join(item.ontology_paths),
        )
    )


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = ["DemoKnowledgeReadModel", "DemoResearchConsoleReadModel"]
