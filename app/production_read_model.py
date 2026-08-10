"""Database-backed, manifest-bound Production corpus read adapter.

The adapter never falls back to Bootstrap files.  It hydrates only the exact
artifact versions named by the active, human-authorized Storage Manifest and
rechecks immutable payload checksums before exposing them to product APIs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC
from threading import RLock
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.knowledge_read_model import (
    CorpusStatistics,
    KnowledgeReadModelError,
    KnowledgeSearchPage,
    ProjectionSummary,
)
from app.runtime_corpus import ActiveCorpus, ActiveCorpusConfigurationError
from knowledge.evidence import EvidenceCard
from knowledge.governance import (
    ClaimEligibility,
    MagicForgeMode,
    ReviewStatus,
    SensitiveInformationLevel,
    StoragePermission,
)
from knowledge.models import KnowledgeRelationship
from knowledge.projections import (
    ArtifactType,
    IngestionReceipt,
    KnowledgeNodeVersion,
    KnowledgeRelationshipAssertion,
    MANIFEST_SCHEMA_VERSION,
    PROJECTION_SCHEMA_VERSION,
    StorageManifest,
)
from persistence.idempotency import canonical_payload_hash
from persistence.models import (
    ClaimCandidate,
    ClaimReviewDecision,
    EvidenceCardVersion,
    GovernanceWorkflowStatus,
    KnowledgeEntityRecord,
    KnowledgeNodeVersionRecord,
    KnowledgeRelationshipAssertionRecord,
    MappingProposalRecord,
    MappingReviewDecision,
    SourceReviewDecision,
    SourceVersion,
)
from persistence.storage_models import (
    ActiveCorpusPointer,
    CorpusActivationState,
    CorpusVersionRecord,
    IngestionReceiptRecord,
    ManifestAuthorizationRecord,
    StorageManifestItemRecord,
    StorageManifestRecord,
    StorageManifestState,
)
from retrieval.interfaces import (
    KnowledgeSearchFilter,
    RetrievalAuthorization,
    SearchResult,
    require_retrieval_authorization,
)
from research.review.source_permission_policy import (
    SourcePermissionPolicyError,
    require_current_source_approval,
)


PRODUCTION_RUNTIME_SCOPE = "production"


def _configuration_error(code: str, message: str) -> ActiveCorpusConfigurationError:
    return ActiveCorpusConfigurationError(code, message)


def load_active_production_corpus(
    settings: Settings,
    session_factory,
    *,
    runtime_scope: str = PRODUCTION_RUNTIME_SCOPE,
) -> ActiveCorpus:
    """Load and verify the unique active Production corpus from SQL."""

    if settings.magicforge_mode != MagicForgeMode.PRODUCTION:
        raise _configuration_error(
            "active_corpus_mode_mismatch",
            "The Production corpus repository cannot be used in Bootstrap mode.",
        )
    if "bootstrap" in settings.qdrant_collection_name.casefold():
        raise _configuration_error(
            "active_corpus_mode_mismatch",
            "Production mode cannot target a Bootstrap collection.",
        )
    if settings.active_qdrant_storage_kind != "remote":
        raise _configuration_error(
            "active_corpus_storage_invalid",
            "Database-backed Production corpora require remote Qdrant storage.",
        )

    scope = runtime_scope.strip()
    if not scope:
        raise _configuration_error(
            "active_corpus_scope_invalid",
            "Production runtime scope is not configured.",
        )

    try:
        with session_factory() as session:
            pointer = session.get(ActiveCorpusPointer, scope)
            if pointer is None:
                raise _configuration_error(
                    "active_corpus_not_configured",
                    "Active corpus identity, manifest, receipt, and schema must be explicit.",
                )
            corpus = session.get(CorpusVersionRecord, pointer.corpus_id)
            if corpus is None or corpus.activation_state != CorpusActivationState.ACTIVE:
                raise _configuration_error(
                    "active_corpus_not_authorized",
                    "The selected Production corpus is not active.",
                )
            manifest_row = session.get(StorageManifestRecord, corpus.manifest_id)
            receipt_row = session.get(
                IngestionReceiptRecord, corpus.ingestion_receipt_id
            )
            authorization = session.scalar(
                select(ManifestAuthorizationRecord).where(
                    ManifestAuthorizationRecord.manifest_id == corpus.manifest_id
                )
            )
            items = list(
                session.scalars(
                    select(StorageManifestItemRecord)
                    .where(StorageManifestItemRecord.manifest_id == corpus.manifest_id)
                    .order_by(StorageManifestItemRecord.sequence_number.asc())
                )
            )
            if manifest_row is None or receipt_row is None or authorization is None:
                raise _configuration_error(
                    "active_corpus_manifest_invalid",
                    "The active Production corpus has incomplete authorization records.",
                )
            if manifest_row.status != StorageManifestState.INGESTED:
                raise _configuration_error(
                    "active_corpus_not_authorized",
                    "The active Production manifest is not ingested.",
                )
            try:
                manifest = StorageManifest.model_validate(manifest_row.manifest_payload)
                receipt = IngestionReceipt.model_validate(receipt_row.receipt_payload)
            except ValidationError as exc:
                raise _configuration_error(
                    "active_corpus_manifest_invalid",
                    "The active Production manifest or receipt is malformed.",
                ) from exc

            _validate_active_records(
                settings=settings,
                corpus=corpus,
                manifest_row=manifest_row,
                authorization=authorization,
                receipt_row=receipt_row,
                manifest=manifest,
                receipt=receipt,
                items=items,
            )

            generated_at = _as_utc_iso(corpus.created_at)
            ingested_at = _as_utc_iso(receipt.ingested_at)
            return ActiveCorpus(
                mode=MagicForgeMode.PRODUCTION,
                corpus_id=str(corpus.id),
                manifest_schema_version=manifest.schema_version,
                projection_schema_version=manifest.projection_schema_version,
                manifest_id=manifest.id,
                manifest_hash=manifest.manifest_hash,
                manifest_path=None,
                receipt_id=receipt.id,
                receipt_path=None,
                collection_name=manifest.collection_name,
                storage_kind="remote",
                local_storage_path=None,
                server_url=settings.qdrant_url,
                artifact_root=None,
                run_summary_path=None,
                smoke_report_path=None,
                expected_point_ids=tuple(manifest.expected_point_ids),
                expected_payload_checksums=tuple(
                    (
                        projection.knowledge_unit_id,
                        projection.payload_checksum,
                    )
                    for projection in manifest.projections
                ),
                expected_point_count=manifest.expected_point_count,
                embedding_dimension=corpus.vector_size,
                authorized=True,
                bootstrap_generated=False,
                human_verified=True,
                generated_at=generated_at,
                ingested_at=ingested_at,
            )
    except ActiveCorpusConfigurationError:
        raise
    except Exception as exc:
        raise _configuration_error(
            "active_corpus_database_unavailable",
            "The active Production corpus could not be verified.",
        ) from exc


def _validate_active_records(
    *,
    settings: Settings,
    corpus: CorpusVersionRecord,
    manifest_row: StorageManifestRecord,
    authorization: ManifestAuthorizationRecord,
    receipt_row: IngestionReceiptRecord,
    manifest: StorageManifest,
    receipt: IngestionReceipt,
    items: list[StorageManifestItemRecord],
) -> None:
    corpus_id = str(corpus.id)
    expected_checksums = {
        projection.knowledge_unit_id: projection.payload_checksum
        for projection in manifest.projections
    }
    failures = (
        manifest.schema_version != MANIFEST_SCHEMA_VERSION,
        manifest.projection_schema_version != PROJECTION_SCHEMA_VERSION,
        str(manifest_row.id) != manifest.id,
        str(manifest_row.corpus_id) != manifest.corpus_id,
        manifest.corpus_id != corpus_id,
        manifest_row.manifest_hash != manifest.manifest_hash,
        authorization.manifest_hash != manifest.manifest_hash,
        str(corpus.manifest_id) != manifest.id,
        str(receipt_row.manifest_id) != manifest.id,
        receipt.manifest_id != manifest.id,
        receipt.corpus_id != corpus_id,
        receipt.manifest_hash != manifest.manifest_hash,
        receipt.collection_name != manifest.collection_name,
        receipt.payload_checksums != expected_checksums,
        tuple(receipt.point_ids) != tuple(manifest.expected_point_ids),
        corpus.qdrant_collection != manifest.collection_name,
        corpus.schema_version != manifest.schema_version,
        corpus.projection_version != manifest.projection_schema_version,
        corpus.vector_distance.casefold() != "cosine",
        manifest.collection_name != settings.qdrant_collection_name,
        bool(settings.active_corpus_id)
        and settings.active_corpus_id != corpus_id,
        settings.active_corpus_manifest_schema
        and settings.active_corpus_manifest_schema != manifest.schema_version,
        len(items) != manifest.expected_point_count,
        {str(item.projection_point_id) for item in items}
        != set(manifest.expected_point_ids),
    )
    if any(failures):
        raise _configuration_error(
            "active_corpus_identity_mismatch",
            "The active Production corpus does not match its authorized manifest.",
        )
    if "bootstrap" in manifest.collection_name.casefold():
        raise _configuration_error(
            "active_corpus_mode_mismatch",
            "Production mode rejected a Bootstrap collection.",
        )
    if receipt.success is not True:
        raise _configuration_error(
            "active_corpus_receipt_mismatch",
            "The active Production ingestion receipt is unsuccessful.",
        )
    if not manifest.authorized or manifest.status.value != "ingested":
        raise _configuration_error(
            "active_corpus_not_authorized",
            "The active Production manifest lacks final human authorization.",
        )
    if (
        manifest.authorization is None
        or manifest.authorization.authorizer_user_id
        != str(authorization.authorizer_user_id)
    ):
        raise _configuration_error(
            "active_corpus_not_authorized",
            "The active Production authorization identity does not match.",
        )
    if canonical_payload_hash(receipt_row.receipt_payload) != receipt_row.payload_checksum:
        raise _configuration_error(
            "active_corpus_receipt_mismatch",
            "The active Production ingestion receipt checksum is invalid.",
        )


@dataclass(frozen=True, slots=True)
class _LoadedArtifact:
    value: EvidenceCard | KnowledgeNodeVersion | KnowledgeRelationshipAssertion
    projection: dict[str, Any]


class ProductionKnowledgeReadModel:
    """Hydrate exactly the artifact versions in one active SQL manifest."""

    def __init__(self, active_corpus: ActiveCorpus, session_factory) -> None:
        if active_corpus.mode != MagicForgeMode.PRODUCTION:
            raise KnowledgeReadModelError(
                "the Production reader requires a Production active corpus"
            )
        if active_corpus.bootstrap_generated or not active_corpus.human_verified:
            raise KnowledgeReadModelError(
                "the Production reader rejected unverified corpus metadata"
            )
        self.active_corpus = active_corpus
        self._session_factory = session_factory
        self._loaded = False
        self._manifest: StorageManifest | None = None
        self._evidence: dict[str, _LoadedArtifact] = {}
        self._nodes: dict[str, _LoadedArtifact] = {}
        self._node_by_identifier: dict[str, _LoadedArtifact] = {}
        self._relationships: dict[str, _LoadedArtifact] = {}
        self._summary: ProjectionSummary | None = None
        self._statistics: CorpusStatistics | None = None
        self._lock = RLock()

    def validate(self) -> None:
        with self._lock:
            self._reload()

    @property
    def summary(self) -> ProjectionSummary:
        with self._lock:
            self._reload()
            assert self._summary is not None
            return self._summary

    @property
    def statistics(self) -> CorpusStatistics:
        with self._lock:
            self._reload()
            assert self._statistics is not None
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
        if auth.bootstrap_limited:
            raise KnowledgeReadModelError(
                "Bootstrap authorization cannot read Production knowledge"
            )
        with self._lock:
            self._reload()
            searchable = (*self._evidence.values(), *self._nodes.values())
            relationship_artifacts = tuple(self._relationships.values())
            assert self._summary is not None
            summary = self._summary
        evidence: list[EvidenceCard] = []
        nodes: list[KnowledgeNodeVersion] = []
        candidates: list[tuple[float, _LoadedArtifact]] = []
        for artifact in searchable:
            if not _projection_visible(artifact.projection, auth):
                continue
            if not _projection_matches_filters(artifact.projection, filters):
                continue
            score = _lexical_score(query, str(artifact.projection.get("text") or ""))
            if query.strip() and score <= 0:
                continue
            candidates.append((score, artifact))
        candidates.sort(key=lambda item: item[0], reverse=True)
        selected = candidates[:limit]
        results = [
            SearchResult(
                text=str(artifact.projection.get("text") or ""),
                score=score,
                payload=_safe_search_projection(artifact),
            )
            for score, artifact in selected
        ]
        for _, artifact in selected:
            if isinstance(artifact.value, EvidenceCard):
                evidence.append(_safe_evidence_card(artifact.value))
            elif isinstance(artifact.value, KnowledgeNodeVersion):
                nodes.append(artifact.value)

        visible_entity_ids = {node.entity.id for node in nodes}
        relationships: list[KnowledgeRelationship] = []
        for artifact in relationship_artifacts:
            assertion = artifact.value
            assert isinstance(assertion, KnowledgeRelationshipAssertion)
            relationship = assertion.relationship
            if (
                _projection_visible(artifact.projection, auth)
                and _projection_matches_filters(artifact.projection, filters)
                and relationship.source_id in visible_entity_ids
                and relationship.target_id in visible_entity_ids
            ):
                relationships.append(relationship)
        return KnowledgeSearchPage(
            results=results,
            evidence_cards=evidence,
            nodes=nodes,  # type: ignore[arg-type] - API accepts versioned Production nodes
            relationships=relationships,
            projection=summary,
        )

    def get_node(
        self,
        identifier: str,
        *,
        authorization: RetrievalAuthorization | None = None,
    ) -> KnowledgeNodeVersion | None:
        auth = require_retrieval_authorization(authorization)
        if auth.bootstrap_limited:
            return None
        with self._lock:
            self._reload()
            artifact = self._node_by_identifier.get(identifier)
        if artifact is None or not _projection_visible(artifact.projection, auth):
            return None
        assert isinstance(artifact.value, KnowledgeNodeVersion)
        return artifact.value

    def get_evidence(
        self,
        identifier: str,
        *,
        authorization: RetrievalAuthorization | None = None,
    ) -> EvidenceCard | None:
        auth = require_retrieval_authorization(authorization)
        if auth.bootstrap_limited:
            return None
        with self._lock:
            self._reload()
            artifact = self._evidence.get(identifier)
        if artifact is None or not _projection_visible(artifact.projection, auth):
            return None
        assert isinstance(artifact.value, EvidenceCard)
        return _safe_evidence_card(artifact.value)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load()
        self._loaded = True

    def _reload(self) -> None:
        """Recheck live activation and approvals before serving cached data."""

        self._loaded = False
        self._manifest = None
        self._evidence = {}
        self._nodes = {}
        self._node_by_identifier = {}
        self._relationships = {}
        self._summary = None
        self._statistics = None
        self._load()
        self._loaded = True

    def _load(self) -> None:
        try:
            with self._session_factory() as session:
                manifest_row = session.get(
                    StorageManifestRecord, UUID(self.active_corpus.manifest_id)
                )
                if manifest_row is None or manifest_row.status != StorageManifestState.INGESTED:
                    raise KnowledgeReadModelError(
                        "the active Production manifest is unavailable"
                    )
                manifest = StorageManifest.model_validate(manifest_row.manifest_payload)
                if (
                    manifest.id != self.active_corpus.manifest_id
                    or manifest.manifest_hash != self.active_corpus.manifest_hash
                    or manifest.corpus_id != self.active_corpus.corpus_id
                ):
                    raise KnowledgeReadModelError(
                        "the Production read manifest identity changed"
                    )
                items = list(
                    session.scalars(
                        select(StorageManifestItemRecord)
                        .where(
                            StorageManifestItemRecord.manifest_id
                            == UUID(manifest.id)
                        )
                        .order_by(StorageManifestItemRecord.sequence_number.asc())
                    )
                )
                if len(items) != manifest.expected_point_count:
                    raise KnowledgeReadModelError(
                        "the Production manifest item set is incomplete"
                    )
                self._validate_live_binding(session, manifest, items)
                projections = {
                    projection.knowledge_unit_id: projection
                    for projection in manifest.projections
                }
                for item in items:
                    projection = projections.get(str(item.projection_point_id))
                    if projection is None:
                        raise KnowledgeReadModelError(
                            "a Production manifest item has no authorized projection"
                        )
                    payload = projection.to_payload(
                        corpus_id=manifest.corpus_id,
                        manifest_id=manifest.id,
                        manifest_hash=manifest.manifest_hash,
                    )
                    self._load_item(session, item, payload)
                self._manifest = manifest
                self._build_summary(manifest)
        except KnowledgeReadModelError:
            raise
        except Exception as exc:
            raise KnowledgeReadModelError(
                "the Production knowledge artifacts are unavailable or inconsistent"
            ) from exc

    def _validate_live_binding(
        self,
        session,
        manifest: StorageManifest,
        items: list[StorageManifestItemRecord],
    ) -> None:
        """Fail closed when corpus activation or receipt identity changes."""

        pointer = session.get(ActiveCorpusPointer, PRODUCTION_RUNTIME_SCOPE)
        corpus = session.get(CorpusVersionRecord, UUID(self.active_corpus.corpus_id))
        receipt_row = session.get(
            IngestionReceiptRecord, UUID(self.active_corpus.receipt_id)
        )
        authorization = session.scalar(
            select(ManifestAuthorizationRecord).where(
                ManifestAuthorizationRecord.manifest_id == UUID(manifest.id)
            )
        )
        if pointer is None or corpus is None or receipt_row is None or authorization is None:
            raise KnowledgeReadModelError(
                "the active Production corpus binding is incomplete"
            )
        try:
            receipt = IngestionReceipt.model_validate(receipt_row.receipt_payload)
        except ValidationError as exc:
            raise KnowledgeReadModelError(
                "the active Production ingestion receipt is malformed"
            ) from exc

        projection_checksums = {
            projection.knowledge_unit_id: projection.payload_checksum
            for projection in manifest.projections
        }
        live_failures = (
            pointer.corpus_id != corpus.id,
            corpus.id != UUID(self.active_corpus.corpus_id),
            corpus.activation_state != CorpusActivationState.ACTIVE,
            corpus.runtime_scope != PRODUCTION_RUNTIME_SCOPE,
            corpus.manifest_id != UUID(manifest.id),
            corpus.ingestion_receipt_id != receipt_row.id,
            corpus.qdrant_collection != manifest.collection_name,
            corpus.schema_version != manifest.schema_version,
            corpus.projection_version != manifest.projection_schema_version,
            corpus.vector_size != self.active_corpus.embedding_dimension,
            corpus.vector_distance.casefold() != "cosine",
            receipt_row.manifest_id != UUID(manifest.id),
            receipt_row.corpus_id != corpus.id,
            receipt_row.manifest_hash != manifest.manifest_hash,
            receipt_row.collection_name != manifest.collection_name,
            canonical_payload_hash(receipt_row.receipt_payload)
            != receipt_row.payload_checksum,
            receipt.id != self.active_corpus.receipt_id,
            receipt.manifest_id != manifest.id,
            receipt.corpus_id != manifest.corpus_id,
            receipt.manifest_hash != manifest.manifest_hash,
            receipt.collection_name != manifest.collection_name,
            receipt.success is not True,
            tuple(receipt.point_ids) != tuple(manifest.expected_point_ids),
            receipt.payload_checksums != projection_checksums,
            tuple(manifest.expected_point_ids) != self.active_corpus.expected_point_ids,
            projection_checksums != dict(self.active_corpus.expected_payload_checksums),
            {str(item.projection_point_id) for item in items}
            != set(manifest.expected_point_ids),
            manifest.authorization is None,
            manifest.authorization is not None
            and manifest.authorization.authorizer_user_id
            != str(authorization.authorizer_user_id),
        )
        if any(live_failures):
            raise KnowledgeReadModelError(
                "the active Production corpus changed; restart is required"
            )

    def _load_item(
        self,
        session,
        item: StorageManifestItemRecord,
        projection: dict[str, Any],
    ) -> None:
        kind = ArtifactType(item.artifact_type)
        if kind == ArtifactType.EVIDENCE_CARD:
            row = session.get(EvidenceCardVersion, item.artifact_row_id)
            model_type = EvidenceCard
        elif kind == ArtifactType.KNOWLEDGE_NODE:
            row = session.get(KnowledgeNodeVersionRecord, item.artifact_row_id)
            model_type = KnowledgeNodeVersion
        elif kind == ArtifactType.RELATIONSHIP:
            row = session.get(
                KnowledgeRelationshipAssertionRecord, item.artifact_row_id
            )
            model_type = KnowledgeRelationshipAssertion
        else:  # pragma: no cover - enum protects this branch
            raise KnowledgeReadModelError("unsupported Production artifact type")
        if row is None:
            raise KnowledgeReadModelError("an authorized Production artifact is missing")
        if (
            canonical_payload_hash(row.payload) != row.payload_checksum
            or row.payload_checksum != item.payload_checksum
            or projection.get("payload_checksum") != item.projection_checksum
        ):
            raise KnowledgeReadModelError(
                "an authorized Production artifact checksum is invalid"
            )
        value = model_type.model_validate(row.payload)
        if (
            str(value.id) != str(item.artifact_domain_id)
            or int(value.version) != item.artifact_version
            or str(projection.get("artifact_id")) != str(item.artifact_domain_id)
            or int(projection.get("artifact_version", 0)) != item.artifact_version
        ):
            raise KnowledgeReadModelError(
                "an authorized Production artifact version does not match its manifest"
            )
        artifact = _LoadedArtifact(value=value, projection=projection)
        if kind == ArtifactType.EVIDENCE_CARD:
            assert isinstance(value, EvidenceCard)
            if not value.projection_eligible:
                raise KnowledgeReadModelError(
                    "the Production manifest contains ineligible Evidence"
                )
            self._require_current_evidence_approval(
                session, row, value, projection
            )
            self._evidence[value.id] = artifact
        elif kind == ArtifactType.KNOWLEDGE_NODE:
            assert isinstance(value, KnowledgeNodeVersion)
            self._require_current_mapping_approval(
                session, row, value=value, relationship=False
            )
            self._nodes[value.id] = artifact
            self._node_by_identifier[value.id] = artifact
            self._node_by_identifier[value.entity.id] = artifact
        else:
            assert isinstance(value, KnowledgeRelationshipAssertion)
            self._require_current_mapping_approval(
                session, row, value=value, relationship=True
            )
            self._relationships[value.id] = artifact

    @staticmethod
    def _require_current_evidence_approval(
        session,
        row: EvidenceCardVersion,
        card: EvidenceCard,
        projection: dict[str, Any],
    ) -> None:
        candidate = session.get(ClaimCandidate, row.claim_candidate_id)
        claim_decision = session.scalar(
            select(ClaimReviewDecision)
            .where(ClaimReviewDecision.claim_candidate_id == row.claim_candidate_id)
            .order_by(ClaimReviewDecision.sequence_number.desc())
            .limit(1)
        )
        source_decision = session.scalar(
            select(SourceReviewDecision)
            .where(SourceReviewDecision.source_version_id == row.source_version_id)
            .order_by(SourceReviewDecision.sequence_number.desc())
            .limit(1)
        )
        latest = session.scalar(
            select(EvidenceCardVersion)
            .where(EvidenceCardVersion.evidence_card_id == row.evidence_card_id)
            .order_by(EvidenceCardVersion.version_number.desc())
            .limit(1)
        )
        if (
            candidate is None
            or candidate.status != GovernanceWorkflowStatus.APPROVED
            or claim_decision is None
            or claim_decision.resulting_status != GovernanceWorkflowStatus.APPROVED
            or claim_decision.id != row.claim_review_decision_id
            or source_decision is None
            or source_decision.resulting_status != GovernanceWorkflowStatus.APPROVED
            or latest is None
            or latest.id != row.id
        ):
            raise KnowledgeReadModelError(
                "an active Production Evidence approval is stale or revoked"
            )

        source_version = session.get(SourceVersion, row.source_version_id)
        if source_version is None:
            raise KnowledgeReadModelError(
                "an active Production Evidence Source version is unavailable"
            )
        try:
            require_current_source_approval(
                session,
                source_version,
                source_decision,
            )
        except SourcePermissionPolicyError as exc:
            raise KnowledgeReadModelError(
                "an active Production Evidence Source approval is invalid: "
                f"{exc.code}"
            ) from exc

        expected_eligibility = min(
            (source_decision.claim_eligibility, claim_decision.claim_eligibility),
            key=_claim_eligibility_rank,
        )
        expected_storage = min(
            (source_decision.storage_permission, claim_decision.storage_permission),
            key=_storage_permission_rank,
        )
        expected_sensitivity = max(
            (
                source_decision.sensitive_information_level,
                claim_decision.sensitive_information_level,
            ),
            key=_sensitivity_rank,
        )
        policy_mismatch = (
            not source_decision.claim_eligibility.allows_projection
            or not claim_decision.claim_eligibility.allows_projection
            or not source_decision.extraction_permission.allows_claim_extraction
            or not source_decision.storage_permission.allows_projection
            or not claim_decision.storage_permission.allows_projection
            or card.review.claim_eligibility != expected_eligibility
            or card.review.claim_eligibility != claim_decision.claim_eligibility
            or card.review.extraction_permission != source_decision.extraction_permission
            or card.review.storage_permission != expected_storage
            or card.review.sensitive_information_level != expected_sensitivity
            or card.knowledge_origin != claim_decision.knowledge_origin
            or card.evidence_class != claim_decision.evidence_class
            or card.evidence_level != claim_decision.evidence_level
            or card.contradiction_status != claim_decision.contradiction_status
            or card.review.contradicting_evidence_checked
            != claim_decision.contradiction_check_status
            or card.secret_exposure_level != claim_decision.secret_exposure_level
            or _sensitivity_rank(card.review.sensitive_information_level)
            < card.secret_exposure_level.rank
            or UUID(card.source.source_version_id) != row.source_version_id
            or card.schema_version != row.domain_schema_version
            or projection.get("sensitive_information_level")
            != expected_sensitivity.value
            or projection.get("secret_exposure_level")
            != card.secret_exposure_level.value
            or int(projection.get("secret_exposure_rank", -1))
            != card.secret_exposure_level.rank
            or projection.get("knowledge_origin") != card.knowledge_origin.value
            or projection.get("evidence_class") != card.evidence_class.value
            or projection.get("evidence_level") != card.evidence_level.value
        )
        if expected_storage == StoragePermission.DERIVED_KNOWLEDGE_ONLY:
            policy_mismatch = policy_mismatch or (
                projection.get("source_locator") != "withheld by storage policy"
                or projection.get("page_number") is not None
            )
        else:
            policy_mismatch = policy_mismatch or (
                projection.get("source_locator") != card.locator.source_locator
                or projection.get("page_number") != card.locator.page_number
            )
        if policy_mismatch:
            raise KnowledgeReadModelError(
                "an active Production Evidence policy is inconsistent"
            )

    @staticmethod
    def _require_current_mapping_approval(
        session, row, *, value, relationship: bool
    ) -> None:
        proposal = session.get(MappingProposalRecord, row.mapping_proposal_id)
        decision = session.scalar(
            select(MappingReviewDecision)
            .where(MappingReviewDecision.mapping_proposal_id == row.mapping_proposal_id)
            .order_by(MappingReviewDecision.sequence_number.desc())
            .limit(1)
        )
        model = (
            KnowledgeRelationshipAssertionRecord
            if relationship
            else KnowledgeNodeVersionRecord
        )
        family_column = model.relationship_id if relationship else model.entity_id
        family_value = row.relationship_id if relationship else row.entity_id
        latest = session.scalar(
            select(model)
            .where(family_column == family_value)
            .order_by(model.version_number.desc())
            .limit(1)
        )
        if (
            proposal is None
            or proposal.status != GovernanceWorkflowStatus.APPROVED
            or decision is None
            or decision.resulting_status != GovernanceWorkflowStatus.APPROVED
            or decision.id != row.mapping_review_decision_id
            or latest is None
            or latest.id != row.id
        ):
            raise KnowledgeReadModelError(
                "an active Production knowledge approval is stale or revoked"
            )
        entity_ids = (
            (
                value.relationship.source_id,
                value.relationship.target_id,
            )
            if relationship
            else (value.entity.id,)
        )
        entities = [session.get(KnowledgeEntityRecord, UUID(item)) for item in entity_ids]
        if any(
            entity is None
            or entity.status.value != "active"
            or entity.merged_into_entity_id is not None
            for entity in entities
        ):
            raise KnowledgeReadModelError(
                "an active Production knowledge entity is inactive or merged"
            )
        if not relationship:
            entity = entities[0]
            if (
                row.entity_id != UUID(value.entity.id)
                or entity.entity_type != value.entity.type
                or entity.canonical_name != value.entity.name
            ):
                raise KnowledgeReadModelError(
                    "an active Production Knowledge Node identity is inconsistent"
                )

    def _build_summary(self, manifest: StorageManifest) -> None:
        projections = manifest.projections
        artifact_types = Counter(item.artifact_type.value for item in projections)
        domain_memberships = Counter(
            domain.value for item in projections for domain in item.domain
        )
        origins = Counter(item.knowledge_origin.value for item in projections)
        knowledge_types = Counter(item.knowledge_type.value for item in projections)
        source_ids = {item.source_id for item in projections}
        entity_ids = {node.value.entity.id for node in self._nodes.values()}
        renderable = sum(
            1
            for item in self._relationships.values()
            if isinstance(item.value, KnowledgeRelationshipAssertion)
            and item.value.relationship.source_id in entity_ids
            and item.value.relationship.target_id in entity_ids
        )
        self._summary = ProjectionSummary(
            run_id=manifest.corpus_id,
            collection=manifest.collection_name,
            manifest_id=manifest.id,
            generated_at=_as_utc_iso(manifest.created_at),
            sources=len(source_ids),
            knowledge_nodes=len(self._nodes),
            relationships=len(self._relationships),
            renderable_relationships=renderable,
            evidence_cards=len(self._evidence),
            qdrant_points=manifest.expected_point_count,
            bootstrap_generated=False,
            human_verified=True,
        )
        self._statistics = CorpusStatistics(
            sources_with_projected_knowledge=len(source_ids),
            source_categories={},
            artifact_types=dict(artifact_types),
            domain_memberships=dict(domain_memberships),
            knowledge_origins=dict(origins),
            knowledge_types=dict(knowledge_types),
            human_verified_points=manifest.expected_point_count,
            contradiction_checks_pending=0,
            pending_human_review_sources=0,
            procedural_method_projections_quarantined=0,
            production_collection_touched=True,
        )


def _projection_visible(
    payload: dict[str, Any], authorization: RetrievalAuthorization
) -> bool:
    return bool(
        payload.get("corpus_id")
        and payload.get("bootstrap_generated") is False
        and payload.get("human_verified") is True
        and payload.get("approved") is True
        and payload.get("claim_eligibility") is True
        and payload.get("storage_permission") is True
        and payload.get("review_status") == ReviewStatus.INGESTED.value
        and payload.get("sensitive_information_level")
        in {item.value for item in authorization.allowed_sensitive_levels}
        and int(payload.get("secret_exposure_rank", 99))
        <= int(authorization.clearance_rank)
    )


def _projection_matches_filters(
    payload: dict[str, Any], filters: KnowledgeSearchFilter
) -> bool:
    checks = {
        "knowledge_type": filters.knowledge_types,
        "domain": filters.domains,
        "ontology_paths": filters.ontology_paths,
        "knowledge_origin": filters.knowledge_origins,
        "evidence_level": filters.evidence_levels,
        "entity_ids": filters.entity_ids,
        "entity_types": filters.entity_types,
        "relation_types": filters.relation_types,
    }
    for key, expected in checks.items():
        if not expected:
            continue
        value = payload.get(key)
        actual = set(value if isinstance(value, list) else [value])
        if not actual.intersection(expected):
            return False
    return True


def _safe_search_projection(artifact: _LoadedArtifact) -> dict[str, Any]:
    """Return a response payload without locator metadata forbidden by review."""

    payload = dict(artifact.projection)
    if (
        isinstance(artifact.value, EvidenceCard)
        and artifact.value.review.storage_permission
        == StoragePermission.DERIVED_KNOWLEDGE_ONLY
    ):
        payload.pop("source_locator", None)
        payload.pop("page_number", None)
    return payload


def _safe_evidence_card(card: EvidenceCard) -> EvidenceCard:
    """Withhold source prose unless the human storage decision allowed it."""

    if card.review.storage_permission == StoragePermission.DERIVED_WITH_SHORT_EXCERPT:
        return card
    payload = card.model_dump(mode="json")
    # The approved derived claim is already visible at this clearance. Reuse
    # it to satisfy the strict EvidenceCard contract without leaking the source
    # prose that the human storage decision withheld.
    payload["evidence_excerpt"] = card.claim
    payload["excerpt_hash"] = ""
    payload["locator"]["source_locator"] = "withheld by storage policy"
    payload["locator"]["media_type"] = "withheld"
    payload["locator"]["page_number"] = None
    payload["locator"]["printed_page"] = None
    payload["locator"]["section"] = None
    payload["locator"]["paragraph"] = None
    payload["locator"]["figure_or_table"] = None
    payload["locator"]["timestamp_start"] = None
    payload["locator"]["timestamp_end"] = None
    return EvidenceCard.model_validate(payload)


def _sensitivity_rank(value: SensitiveInformationLevel) -> int:
    return {
        SensitiveInformationLevel.PUBLIC: 0,
        SensitiveInformationLevel.CONTROLLED: 1,
        SensitiveInformationLevel.SECRET_METHOD: 2,
        SensitiveInformationLevel.RESTRICTED: 3,
    }[value]


def _storage_permission_rank(value: StoragePermission) -> int:
    return {
        StoragePermission.NONE: 0,
        StoragePermission.DERIVED_KNOWLEDGE_ONLY: 1,
        StoragePermission.DERIVED_WITH_SHORT_EXCERPT: 2,
    }[value]


def _claim_eligibility_rank(value: ClaimEligibility) -> int:
    return {
        ClaimEligibility.NOT_ASSESSED: 0,
        ClaimEligibility.INELIGIBLE: 0,
        ClaimEligibility.ELIGIBLE_WITH_LIMITS: 1,
        ClaimEligibility.ELIGIBLE: 2,
    }[value]


def _lexical_score(query: str, text: str) -> float:
    terms = {item for item in query.casefold().split() if item}
    if not terms:
        return 1.0
    haystack = text.casefold()
    matches = sum(1 for term in terms if term in haystack)
    return matches / len(terms)


def _as_utc_iso(value) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


__all__ = [
    "PRODUCTION_RUNTIME_SCOPE",
    "ProductionKnowledgeReadModel",
    "load_active_production_corpus",
]
