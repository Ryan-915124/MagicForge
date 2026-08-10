"""Read-only API projection over an audited MagicForge bootstrap run.

The read model never invents nodes or edges. It validates the canonical
registry, then intersects every artifact with the Qdrant ingestion manifest so
only points that were actually projected into the isolated collection can be
returned to the frontend.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from app.runtime_corpus import ActiveCorpus
from knowledge.bootstrap import (
    BootstrapKnowledgeNodeCandidate,
    BootstrapRelationshipCandidate,
)
from knowledge.evidence import EvidenceCard
from knowledge.governance import MagicForgeMode, SensitiveInformationLevel
from knowledge.models import KnowledgeRelationship
from retrieval.interfaces import (
    KnowledgeSearchFilter,
    RetrievalAuthorization,
    SearchResult,
    require_retrieval_authorization,
)


class KnowledgeReadModelError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectionSummary:
    run_id: str
    collection: str
    manifest_id: str
    generated_at: str
    sources: int
    knowledge_nodes: int
    relationships: int
    renderable_relationships: int
    evidence_cards: int
    qdrant_points: int
    bootstrap_generated: bool = True
    human_verified: bool = False


@dataclass(frozen=True, slots=True)
class CorpusStatistics:
    """Audited statistics derived from one projected run.

    Projection distributions are calculated from payloads that have already
    passed the bootstrap safety gate. Source categories use registered source
    records, so their denominator remains separate from projected points.
    """

    sources_with_projected_knowledge: int
    source_categories: dict[str, int]
    artifact_types: dict[str, int]
    domain_memberships: dict[str, int]
    knowledge_origins: dict[str, int]
    knowledge_types: dict[str, int]
    human_verified_points: int
    contradiction_checks_pending: int
    pending_human_review_sources: int
    procedural_method_projections_quarantined: int
    production_collection_touched: bool


@dataclass(frozen=True, slots=True)
class KnowledgeSearchPage:
    results: list[SearchResult]
    evidence_cards: list[EvidenceCard]
    nodes: list[BootstrapKnowledgeNodeCandidate]
    relationships: list[KnowledgeRelationship]
    projection: ProjectionSummary


class ProjectedKnowledgeReadModel:
    """Hydrate graph and evidence DTOs from one immutable projected run."""

    def __init__(self, active_corpus: ActiveCorpus) -> None:
        if active_corpus.mode != MagicForgeMode.BOOTSTRAP:
            raise KnowledgeReadModelError(
                "the projected Bootstrap reader requires a Bootstrap active corpus"
            )
        if active_corpus.run_summary_path is None:
            raise KnowledgeReadModelError(
                "the active Bootstrap corpus has no run summary"
            )
        self.active_corpus = active_corpus
        self._loaded = False
        self._nodes: list[BootstrapKnowledgeNodeCandidate] = []
        self._relationships: list[BootstrapRelationshipCandidate] = []
        self._evidence_cards: list[EvidenceCard] = []
        self._node_projection: dict[str, dict[str, Any]] = {}
        self._relationship_projection: dict[str, dict[str, Any]] = {}
        self._evidence_projection: dict[str, dict[str, Any]] = {}
        self._node_by_identifier: dict[str, BootstrapKnowledgeNodeCandidate] = {}
        self._evidence_by_id: dict[str, EvidenceCard] = {}
        self._summary: ProjectionSummary | None = None
        self._statistics: CorpusStatistics | None = None

    def validate(self) -> None:
        """Load every read artifact and prove it matches the active corpus."""

        self._ensure_loaded()
        summary = self.summary
        expected = self.active_corpus
        mismatches = {
            "corpus_id": (summary.run_id, expected.corpus_id),
            "manifest_id": (summary.manifest_id, expected.manifest_id),
            "collection": (summary.collection, expected.collection_name),
            "point_count": (summary.qdrant_points, expected.expected_point_count),
            "bootstrap_generated": (
                summary.bootstrap_generated,
                expected.bootstrap_generated,
            ),
            "human_verified": (summary.human_verified, expected.human_verified),
        }
        for field, (observed, configured) in mismatches.items():
            if observed != configured:
                raise KnowledgeReadModelError(
                    f"projected read model {field} does not match the active corpus"
                )

    @property
    def summary(self) -> ProjectionSummary:
        self._ensure_loaded()
        if self._summary is None:  # pragma: no cover - guarded by _load
            raise KnowledgeReadModelError("projection summary was not loaded")
        return self._summary

    @property
    def statistics(self) -> CorpusStatistics:
        self._ensure_loaded()
        if self._statistics is None:  # pragma: no cover - guarded by _load
            raise KnowledgeReadModelError("corpus statistics were not loaded")
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
        self._ensure_loaded()
        include_evidence = not filters.knowledge_types or "evidence" in filters.knowledge_types
        include_graph = not filters.knowledge_types or any(
            value != "evidence" for value in filters.knowledge_types
        )

        evidence_cards = (
            self._search_evidence(query, limit, filters, auth)
            if include_evidence
            else []
        )
        nodes, relationships = (
            self._search_graph(query, limit, filters, auth)
            if include_graph
            else ([], [])
        )

        results: list[SearchResult] = []
        for card in evidence_cards:
            projection = self._evidence_projection[card.id]
            results.append(
                SearchResult(
                    text=str(projection.get("text") or card.claim),
                    score=_lexical_score(query, _card_text(card)),
                    payload=projection,
                )
            )
        for node in nodes:
            projection = self._node_projection[node.id]
            results.append(
                SearchResult(
                    text=str(projection.get("text") or node.definition),
                    score=_lexical_score(query, _node_text(node)),
                    payload=projection,
                )
            )

        results.sort(key=lambda item: item.score, reverse=True)
        return KnowledgeSearchPage(
            results=results[:limit],
            evidence_cards=evidence_cards,
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
        self._ensure_loaded()
        item = self._node_by_identifier.get(identifier)
        if item is None or not _projection_is_authorized(
            self._node_projection.get(item.id), auth
        ):
            return None
        return item

    def get_evidence(
        self,
        identifier: str,
        *,
        authorization: RetrievalAuthorization | None = None,
    ) -> EvidenceCard | None:
        auth = require_retrieval_authorization(authorization)
        self._ensure_loaded()
        item = self._evidence_by_id.get(identifier)
        if item is None or not _projection_is_authorized(
            self._evidence_projection.get(item.id), auth
        ):
            return None
        return item

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load()
        self._loaded = True

    def _load(self) -> None:
        corpus = self.active_corpus
        registry_path = corpus.artifact_root / "knowledge_nodes/_canonical_registry.json"
        manifest_path = corpus.manifest_path
        report_path = corpus.run_summary_path
        assert report_path is not None  # checked by the constructor
        evidence_path = corpus.artifact_root / "evidence_cards"
        sources_path = corpus.artifact_root / "sources"
        for path in (
            registry_path,
            manifest_path,
            report_path,
            evidence_path,
            sources_path,
        ):
            if not path.exists():
                raise KnowledgeReadModelError(
                    f"required projected artifact is missing: {path.name}"
                )

        registry = _read_json(registry_path)
        manifest = _read_json(manifest_path)
        report = _read_json(report_path)
        projections = manifest.get("projections")
        if not isinstance(projections, list):
            raise KnowledgeReadModelError("Qdrant manifest has no projection list")
        identity = {
            "corpus_id": (report.get("run_id"), corpus.corpus_id),
            "manifest_schema_version": (
                manifest.get("schema_version"),
                corpus.manifest_schema_version,
            ),
            "manifest_id": (manifest.get("id"), corpus.manifest_id),
            "manifest_hash": (manifest.get("manifest_hash"), corpus.manifest_hash),
            "collection": (manifest.get("collection_name"), corpus.collection_name),
            "run_collection": (report.get("collection"), corpus.collection_name),
            "point_count": (len(projections), corpus.expected_point_count),
        }
        for field, (observed, expected) in identity.items():
            if observed != expected:
                raise KnowledgeReadModelError(
                    f"projected artifact {field} does not match the active corpus"
                )

        by_artifact_type: dict[str, dict[str, dict[str, Any]]] = {
            "evidence_card": {},
            "knowledge_node": {},
            "relationship": {},
        }
        for projection in projections:
            if not isinstance(projection, dict):
                raise KnowledgeReadModelError("Qdrant manifest contains a malformed projection")
            if projection.get("schema_version") != corpus.projection_schema_version:
                raise KnowledgeReadModelError(
                    "projection schema does not match the active corpus"
                )
            _validate_projection_safety(projection)
            artifact_type = str(projection.get("artifact_type") or "")
            artifact_id = str(projection.get("artifact_id") or "")
            if artifact_type in by_artifact_type and artifact_id:
                by_artifact_type[artifact_type][artifact_id] = projection

        artifact_types: Counter[str] = Counter()
        domain_memberships: Counter[str] = Counter()
        knowledge_origins: Counter[str] = Counter()
        knowledge_types: Counter[str] = Counter()
        review_statuses: Counter[str] = Counter()
        human_verified_points = 0
        contradiction_checks_pending = 0
        for projection in projections:
            artifact_type = _required_projection_label(projection, "artifact_type")
            knowledge_origin = _required_projection_label(projection, "knowledge_origin")
            knowledge_type = _required_projection_label(projection, "knowledge_type")
            review_status = _required_projection_label(projection, "review_status")
            artifact_types[artifact_type] += 1
            knowledge_origins[knowledge_origin] += 1
            knowledge_types[knowledge_type] += 1
            review_statuses[review_status] += 1
            for domain in _projection_labels(projection, "domain"):
                domain_memberships[domain] += 1
            if projection.get("human_verified") is True:
                human_verified_points += 1
            if (
                artifact_type == "evidence_card"
                and projection.get("contradiction_status") == "not_checked"
            ):
                contradiction_checks_pending += 1

        node_adapter = TypeAdapter(list[BootstrapKnowledgeNodeCandidate])
        relationship_adapter = TypeAdapter(list[BootstrapRelationshipCandidate])
        raw_nodes = node_adapter.validate_python(registry.get("nodes", []))
        raw_relationships = relationship_adapter.validate_python(
            registry.get("relationships", [])
        )
        self._node_projection = by_artifact_type["knowledge_node"]
        self._relationship_projection = by_artifact_type["relationship"]
        self._evidence_projection = by_artifact_type["evidence_card"]
        self._nodes = [item for item in raw_nodes if item.id in self._node_projection]
        self._relationships = [
            item for item in raw_relationships if item.id in self._relationship_projection
        ]

        entity_ids = {item.entity.id for item in self._nodes}
        self._relationships = [
            item
            for item in self._relationships
            if item.relationship.source_id in entity_ids
            and item.relationship.target_id in entity_ids
        ]
        self._node_by_identifier = {
            identifier: item
            for item in self._nodes
            for identifier in (item.id, item.entity.id)
        }

        evidence_adapter = TypeAdapter(list[EvidenceCard])
        cards: list[EvidenceCard] = []
        for path in sorted(evidence_path.glob("*.json")):
            cards.extend(evidence_adapter.validate_python(_read_json(path)))
        self._evidence_cards = [
            item for item in cards if item.id in self._evidence_projection
        ]
        self._evidence_by_id = {item.id: item for item in self._evidence_cards}

        counts = report.get("counts", {})
        if not isinstance(counts, dict):
            raise KnowledgeReadModelError("run summary contains malformed counts")
        safety = report.get("safety", {})
        review_queue = report.get("human_review_queue", {})
        if not isinstance(safety, dict) or not isinstance(review_queue, dict):
            raise KnowledgeReadModelError("run summary contains malformed governance data")

        source_categories: Counter[str] = Counter()
        pending_human_review_sources = 0
        source_records = 0
        for path in sorted(sources_path.glob("*.json")):
            source_record = _read_json(path)
            if not isinstance(source_record, dict) or not source_record.get("candidate_id"):
                continue
            source_records += 1
            category = str(source_record.get("source_category") or "unclassified")
            source_categories[category] += 1
            if source_record.get("human_verified") is not True:
                pending_human_review_sources += 1

        self._summary = ProjectionSummary(
            run_id=corpus.corpus_id,
            collection=corpus.collection_name,
            manifest_id=corpus.manifest_id,
            generated_at=str(report.get("generated_at") or ""),
            sources=int(counts.get("sources_processed_total", 0)),
            knowledge_nodes=len(self._nodes),
            relationships=len(self._relationship_projection),
            renderable_relationships=len(self._relationships),
            evidence_cards=len(self._evidence_cards),
            qdrant_points=len(projections),
            bootstrap_generated=corpus.bootstrap_generated,
            human_verified=corpus.human_verified,
        )

        if source_records != self._summary.sources:
            raise KnowledgeReadModelError(
                "registered source count does not match the run summary"
            )

        self._statistics = CorpusStatistics(
            sources_with_projected_knowledge=int(
                counts.get("sources_with_projected_knowledge", 0)
            ),
            source_categories=dict(sorted(source_categories.items())),
            artifact_types=dict(sorted(artifact_types.items())),
            domain_memberships=dict(sorted(domain_memberships.items())),
            knowledge_origins=dict(sorted(knowledge_origins.items())),
            knowledge_types=dict(sorted(knowledge_types.items())),
            human_verified_points=human_verified_points,
            contradiction_checks_pending=contradiction_checks_pending,
            pending_human_review_sources=pending_human_review_sources,
            procedural_method_projections_quarantined=int(
                review_queue.get("procedural_method_projections_quarantined", 0)
            ),
            production_collection_touched=bool(
                safety.get("production_collection_touched", False)
            ),
        )

        expected = {
            "knowledge_nodes_projected": len(self._nodes),
            "relationships_projected": len(self._relationship_projection),
            "evidence_cards_projected": len(self._evidence_cards),
            "qdrant_points_created": len(projections),
        }
        for key, value in expected.items():
            if counts.get(key) != value:
                raise KnowledgeReadModelError(
                    f"run summary count {key} does not match projected artifacts"
                )

        governance_expected = {
            "sources_requiring_human_review": pending_human_review_sources,
            "contradiction_checks_pending": contradiction_checks_pending,
        }
        for key, value in governance_expected.items():
            if counts.get(key) != value:
                raise KnowledgeReadModelError(
                    f"run summary count {key} does not match projected artifacts"
                )

        if safety.get("human_verified_points") != human_verified_points:
            raise KnowledgeReadModelError(
                "run summary human verification count does not match projections"
            )
        if safety.get("review_status_counts") != dict(review_statuses):
            raise KnowledgeReadModelError(
                "run summary review status counts do not match projections"
            )

    def _search_evidence(
        self,
        query: str,
        limit: int,
        filters: KnowledgeSearchFilter,
        authorization: RetrievalAuthorization,
    ) -> list[EvidenceCard]:
        candidates = [
            item
            for item in self._evidence_cards
            if _projection_is_authorized(
                self._evidence_projection.get(item.id), authorization
            )
            and _matches_common_filters(
                origin=item.knowledge_origin.value,
                domains=[value.value for value in item.applicable_domain],
                evidence_level=item.evidence_level.value,
                ontology_paths=item.ontology_paths,
                filters=filters,
            )
        ]
        return _ranked(query, candidates, _card_text, limit)

    def _search_graph(
        self,
        query: str,
        limit: int,
        filters: KnowledgeSearchFilter,
        authorization: RetrievalAuthorization,
    ) -> tuple[
        list[BootstrapKnowledgeNodeCandidate],
        list[BootstrapRelationshipCandidate],
    ]:
        candidates = [
            item
            for item in self._nodes
            if _projection_is_authorized(
                self._node_projection.get(item.id), authorization
            )
            and _matches_common_filters(
                origin=item.knowledge_origin.value,
                domains=[value.value for value in item.domains],
                evidence_level=str(
                    self._node_projection[item.id].get("evidence_level") or ""
                ),
                ontology_paths=item.ontology_paths,
                filters=filters,
            )
            and (
                not filters.knowledge_types
                or str(self._node_projection[item.id].get("knowledge_type") or "")
                in filters.knowledge_types
            )
            and (
                not filters.entity_types
                or item.entity.type.value in filters.entity_types
            )
            and (
                not filters.entity_ids or item.entity.id in filters.entity_ids
            )
        ]
        candidate_by_id = {item.entity.id: item for item in candidates}
        allowed_relationships = [
            item
            for item in self._relationships
            if _projection_is_authorized(
                self._relationship_projection.get(item.id), authorization
            )
            and (
                not filters.relation_types
                or item.relationship.type.value in filters.relation_types
            )
        ]

        if query.strip():
            ranked = _ranked(query, candidates, _node_text, limit)
            matched_ids = {item.entity.id for item in ranked}
            for relationship in allowed_relationships:
                relation_score = _lexical_score(query, _relationship_text(relationship))
                touches_match = bool(
                    matched_ids
                    & {
                        relationship.relationship.source_id,
                        relationship.relationship.target_id,
                    }
                )
                if relation_score <= 0 and not touches_match:
                    continue
                for identifier in (
                    relationship.relationship.source_id,
                    relationship.relationship.target_id,
                ):
                    neighbor = candidate_by_id.get(identifier)
                    if neighbor and neighbor not in ranked and len(ranked) < limit:
                        ranked.append(neighbor)
            selected = ranked
        else:
            connected_ids = {
                identifier
                for item in allowed_relationships
                for identifier in (
                    item.relationship.source_id,
                    item.relationship.target_id,
                )
            }
            selected = sorted(
                candidates,
                key=lambda item: (
                    item.entity.id not in connected_ids,
                    -item.confidence.score,
                    item.entity.type.value,
                    item.entity.name.casefold(),
                ),
            )[:limit]

        selected_ids = {item.entity.id for item in selected}
        relationships = [
            item
            for item in allowed_relationships
            if item.relationship.source_id in selected_ids
            and item.relationship.target_id in selected_ids
        ]
        return selected, relationships


def _projection_is_authorized(
    projection: dict[str, Any] | None,
    authorization: RetrievalAuthorization,
) -> bool:
    """Fail closed when a projection lacks complete security metadata."""

    if projection is None:
        return False
    try:
        sensitivity = SensitiveInformationLevel(
            str(projection["sensitive_information_level"])
        )
        exposure_rank = int(projection["secret_exposure_rank"])
    except (KeyError, TypeError, ValueError):
        return False
    if not 0 <= exposure_rank <= 3:
        return False
    return (
        sensitivity in authorization.allowed_sensitive_levels
        and exposure_rank <= int(authorization.clearance_rank)
    )


def _matches_common_filters(
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
        and (
            not filters.evidence_levels
            or evidence_level in filters.evidence_levels
        )
        and (
            not filters.ontology_paths
            or bool(set(ontology_paths) & set(filters.ontology_paths))
        )
    )


def _ranked(query: str, values: list[Any], text_fn, limit: int) -> list[Any]:
    if not query.strip():
        return values[:limit]
    scored = [(_lexical_score(query, text_fn(item)), item) for item in values]
    return [
        item
        for score, item in sorted(
            scored,
            key=lambda pair: pair[0],
            reverse=True,
        )
        if score > 0
    ][:limit]


def _lexical_score(query: str, text: str) -> float:
    normalized_query = " ".join(query.casefold().split())
    if not normalized_query:
        return 1.0
    haystack = " ".join(text.casefold().split())
    terms = re.findall(r"[\w-]+", normalized_query, flags=re.UNICODE)
    if not terms:
        return 0.0
    matched = sum(1 for term in terms if term in haystack)
    if matched == 0:
        return 0.0
    phrase_bonus = 1.0 if normalized_query in haystack else 0.0
    return min(1.0, (matched / len(terms)) * 0.8 + phrase_bonus * 0.2)


def _node_text(item: BootstrapKnowledgeNodeCandidate) -> str:
    return " ".join(
        (
            item.entity.name,
            item.definition,
            item.entity.description or "",
            " ".join(item.entity.aliases),
            " ".join(item.topic_tags),
            " ".join(item.ontology_paths),
        )
    )


def _card_text(item: EvidenceCard) -> str:
    return " ".join(
        (
            item.claim,
            item.evidence_excerpt,
            item.magic_application or "",
            " ".join(item.topic_tags),
            " ".join(item.ontology_paths),
        )
    )


def _relationship_text(item: BootstrapRelationshipCandidate) -> str:
    return " ".join(
        (
            item.source_entity_name,
            item.relationship.type.value,
            item.target_entity_name,
            item.assertion,
            " ".join(item.topic_tags),
        )
    )


def _validate_projection_safety(projection: dict[str, Any]) -> None:
    if projection.get("bootstrap_generated") is not True:
        raise KnowledgeReadModelError("projection is missing its bootstrap marker")
    if projection.get("human_verified") is not False:
        raise KnowledgeReadModelError("bootstrap projection claims human verification")
    if projection.get("review_status") != "bootstrap":
        raise KnowledgeReadModelError("projection escaped bootstrap review state")
    if int(projection.get("secret_exposure_rank", 99)) > 1:
        raise KnowledgeReadModelError("projection exceeds general-principle clearance")


def _required_projection_label(projection: dict[str, Any], field: str) -> str:
    value = projection.get(field)
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeReadModelError(
            f"projection is missing the required {field} label"
        )
    return value


def _projection_labels(projection: dict[str, Any], field: str) -> tuple[str, ...]:
    values = projection.get(field, [])
    if not isinstance(values, list):
        raise KnowledgeReadModelError(f"projection contains malformed {field} labels")
    labels = [str(value).strip() for value in values if str(value).strip()]
    return tuple(dict.fromkeys(labels))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeReadModelError(
            f"could not read projected artifact: {path.name}"
        ) from exc
