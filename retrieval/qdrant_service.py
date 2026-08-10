"""Qdrant adapter for authorized structured knowledge projections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import NoReturn

from qdrant_client import QdrantClient, models

from knowledge.bootstrap import BootstrapQdrantProjection
from knowledge.projections import (
    IngestionReceipt,
    QdrantProjection,
    StorageManifest,
)
from knowledge.governance import MagicForgeMode, ReviewStatus, require_human_identity
from retrieval.embeddings import EmbeddingProvider
from retrieval.interfaces import (
    KnowledgeSearchFilter,
    RetrievalAuthorization,
    SearchResult,
    require_retrieval_authorization,
)


class QdrantServiceError(RuntimeError):
    pass


class QdrantService:
    """Read approved projections and write only authorized storage manifests."""

    _PAYLOAD_INDEXES = {
        "schema_version": models.PayloadSchemaType.KEYWORD,
        "corpus_id": models.PayloadSchemaType.KEYWORD,
        "knowledge_unit_id": models.PayloadSchemaType.KEYWORD,
        "artifact_type": models.PayloadSchemaType.KEYWORD,
        "artifact_id": models.PayloadSchemaType.KEYWORD,
        "knowledge_type": models.PayloadSchemaType.KEYWORD,
        "domain": models.PayloadSchemaType.KEYWORD,
        "ontology_paths": models.PayloadSchemaType.KEYWORD,
        "topic_tags": models.PayloadSchemaType.KEYWORD,
        "knowledge_origin": models.PayloadSchemaType.KEYWORD,
        "evidence_level": models.PayloadSchemaType.KEYWORD,
        "evidence_class": models.PayloadSchemaType.KEYWORD,
        "claim_roles": models.PayloadSchemaType.KEYWORD,
        "confidence_label": models.PayloadSchemaType.KEYWORD,
        "source_type": models.PayloadSchemaType.KEYWORD,
        "source_id": models.PayloadSchemaType.KEYWORD,
        "citation_id": models.PayloadSchemaType.KEYWORD,
        "source_candidate_id": models.PayloadSchemaType.KEYWORD,
        "document_id": models.PayloadSchemaType.KEYWORD,
        "evidence_card_id": models.PayloadSchemaType.KEYWORD,
        "canonical_claim_id": models.PayloadSchemaType.KEYWORD,
        "supporting_evidence_ids": models.PayloadSchemaType.KEYWORD,
        "contradicting_evidence_ids": models.PayloadSchemaType.KEYWORD,
        "contradiction_status": models.PayloadSchemaType.KEYWORD,
        "entity_ids": models.PayloadSchemaType.KEYWORD,
        "entity_types": models.PayloadSchemaType.KEYWORD,
        "relationship_ids": models.PayloadSchemaType.KEYWORD,
        "relation_types": models.PayloadSchemaType.KEYWORD,
        "secret_exposure_level": models.PayloadSchemaType.KEYWORD,
        "sensitive_information_level": models.PayloadSchemaType.KEYWORD,
        "review_status": models.PayloadSchemaType.KEYWORD,
        "source_review_status": models.PayloadSchemaType.KEYWORD,
        "verification_status": models.PayloadSchemaType.KEYWORD,
        "review_item_id": models.PayloadSchemaType.KEYWORD,
        "claim_review_item_ids": models.PayloadSchemaType.KEYWORD,
        "storage_manifest_id": models.PayloadSchemaType.KEYWORD,
        "manifest_hash": models.PayloadSchemaType.KEYWORD,
        "claim_eligibility": models.PayloadSchemaType.BOOL,
        "storage_permission": models.PayloadSchemaType.BOOL,
        "approved": models.PayloadSchemaType.BOOL,
        "bootstrap_generated": models.PayloadSchemaType.BOOL,
        "human_verified": models.PayloadSchemaType.BOOL,
        "confidence": models.PayloadSchemaType.FLOAT,
        "page_number": models.PayloadSchemaType.INTEGER,
        "source_year": models.PayloadSchemaType.INTEGER,
        "secret_exposure_rank": models.PayloadSchemaType.INTEGER,
        "artifact_version": models.PayloadSchemaType.INTEGER,
        "content_version": models.PayloadSchemaType.INTEGER,
    }

    def __init__(
        self,
        url: str,
        collection_name: str,
        embedding_provider: EmbeddingProvider,
        client: QdrantClient | None = None,
        mode: MagicForgeMode = MagicForgeMode.PRODUCTION,
        *,
        active_corpus_id: str | None = None,
        active_manifest_id: str | None = None,
        active_manifest_hash: str | None = None,
        active_projection_schema: str | None = None,
        active_payload_checksums: Mapping[str, str] | None = None,
        production_writes_enabled: bool = False,
    ) -> None:
        self.url = url
        self.collection_name = collection_name
        self.embedding_provider = embedding_provider
        self._client = client
        self.mode = mode
        self.active_corpus_id = (active_corpus_id or "").strip()
        self.active_manifest_id = (active_manifest_id or "").strip()
        self.active_manifest_hash = (active_manifest_hash or "").strip()
        self.active_projection_schema = (active_projection_schema or "").strip()
        self.active_payload_checksums = dict(active_payload_checksums or {})
        self.production_writes_enabled = bool(production_writes_enabled)
        self._closed = False

    @property
    def client(self) -> QdrantClient:
        if self._closed:
            raise QdrantServiceError("Qdrant service is closed")
        if self._client is None:
            self._client = QdrantClient(url=self.url)
        return self._client

    def validate_readiness(
        self,
        *,
        expected_point_count: int,
        expected_point_ids: Sequence[str],
        expected_payload_checksums: Mapping[str, str],
        corpus_id: str = "",
        manifest_id: str,
        manifest_hash: str,
        expected_projection_schema: str,
    ) -> None:
        """Validate an existing collection without creating or changing it."""

        if expected_point_count < 0:
            raise QdrantServiceError("expected Qdrant point count cannot be negative")
        expected_ids, expected_checksums = self._normalize_expected_points(
            expected_point_count,
            expected_point_ids,
            expected_payload_checksums,
        )
        if (
            self.active_payload_checksums
            and expected_checksums != self.active_payload_checksums
        ):
            raise QdrantServiceError(
                "readiness checksums do not match the configured active manifest"
            )
        identity = {
            "corpus_id": corpus_id.strip(),
            "manifest_id": manifest_id.strip(),
            "manifest_hash": manifest_hash.strip(),
            "projection_schema": expected_projection_schema.strip(),
        }
        required_identity = (
            identity
            if self.mode == MagicForgeMode.PRODUCTION
            else {key: value for key, value in identity.items() if key != "corpus_id"}
        )
        missing = [name for name, value in required_identity.items() if not value]
        if missing:
            raise QdrantServiceError(
                "Qdrant readiness requires " + ", ".join(sorted(missing))
            )

        try:
            if not self.client.collection_exists(self.collection_name):
                raise QdrantServiceError(
                    f"Qdrant collection {self.collection_name!r} is not available"
                )

            collection = self.client.get_collection(self.collection_name)
            vectors = collection.config.params.vectors
            size = getattr(vectors, "size", None)
            if size != self.embedding_provider.dimension:
                raise QdrantServiceError(
                    f"collection vector size is {size}, but EMBEDDING_DIMENSION is "
                    f"{self.embedding_provider.dimension}"
                )
            distance = getattr(vectors, "distance", None)
            if distance != models.Distance.COSINE:
                raise QdrantServiceError(
                    "configured Qdrant collection does not use cosine distance"
                )

            total_count = self.client.count(
                collection_name=self.collection_name,
                exact=True,
            ).count
            if total_count != expected_point_count:
                raise QdrantServiceError(
                    f"Qdrant collection contains {total_count} points; "
                    f"active manifest expects {expected_point_count}"
                )

            matching_count = self.client.count(
                collection_name=self.collection_name,
                count_filter=self._readiness_filter(
                    corpus_id=identity["corpus_id"],
                    manifest_id=identity["manifest_id"],
                    manifest_hash=identity["manifest_hash"],
                    projection_schema=identity["projection_schema"],
                    mode=self.mode,
                ),
                exact=True,
            ).count
            if matching_count != expected_point_count:
                raise QdrantServiceError(
                    f"Qdrant collection contains {matching_count} of "
                    f"{expected_point_count} points for the active manifest and mode"
                )
            self._verify_active_points(
                expected_ids=expected_ids,
                expected_checksums=expected_checksums,
                identity=identity,
                mode=self.mode,
            )
        except QdrantServiceError:
            raise
        except Exception as exc:
            raise QdrantServiceError(
                "could not validate the configured Qdrant collection"
            ) from exc

    def close(self) -> None:
        """Close the underlying client once and prohibit later lazy reopening."""

        if self._closed:
            return
        self._closed = True
        self._embed_query_cached.cache_clear()
        if self._client is None:
            return
        try:
            self._client.close()
        except Exception as exc:
            raise QdrantServiceError("could not close the Qdrant client") from exc

    def create_collection(self) -> bool:
        self._require_production_write_capability()
        try:
            if self.client.collection_exists(self.collection_name):
                self._validate_existing_dimension()
                self._ensure_payload_indexes()
                return False
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=self.embedding_provider.dimension,
                    distance=models.Distance.COSINE,
                ),
            )
            self._ensure_payload_indexes()
            return True
        except QdrantServiceError:
            raise
        except Exception as exc:
            raise QdrantServiceError(f"could not initialize Qdrant: {exc}") from exc

    def _ensure_payload_indexes(self) -> None:
        collection = self.client.get_collection(self.collection_name)
        existing = set(getattr(collection, "payload_schema", {}) or {})
        for field_name, field_schema in self._PAYLOAD_INDEXES.items():
            if field_name not in existing:
                self.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field_name,
                    field_schema=field_schema,
                    wait=True,
                )

    def _validate_existing_dimension(self) -> None:
        collection = self.client.get_collection(self.collection_name)
        vectors = collection.config.params.vectors
        size = getattr(vectors, "size", None)
        if size is not None and size != self.embedding_provider.dimension:
            raise QdrantServiceError(
                f"collection vector size is {size}, but EMBEDDING_DIMENSION is "
                f"{self.embedding_provider.dimension}"
            )
        distance = getattr(vectors, "distance", None)
        if distance is not None and distance != models.Distance.COSINE:
            raise QdrantServiceError(
                "configured Qdrant collection does not use cosine distance"
            )

    def add_documents(self, documents: Sequence[object]) -> NoReturn:
        """Fail closed for the removed raw KnowledgeChunk ingestion contract."""

        del documents
        raise QdrantServiceError(
            "raw document/chunk ingestion is disabled; use an authorized StorageManifest"
        )

    def write_manifest(
        self,
        manifest: StorageManifest,
        *,
        actor: str,
    ) -> IngestionReceipt:
        if self.mode != MagicForgeMode.PRODUCTION:
            raise QdrantServiceError(
                "production StorageManifest writes are disabled in bootstrap mode"
            )
        self._require_production_write_capability()
        self._assert_production_collection()
        try:
            operator = require_human_identity(actor)
        except ValueError as exc:
            raise QdrantServiceError(str(exc)) from exc
        if not manifest.authorized:
            raise QdrantServiceError("only an authorized StorageManifest may be written")
        if manifest.collection_name != self.collection_name:
            raise QdrantServiceError("manifest targets a different Qdrant collection")
        if self._manifest_is_fully_ingested(manifest):
            return self._receipt_for_manifest(manifest, operator)
        created = self.create_collection()
        if not created:
            self._require_collection_bound_to_manifest(manifest)
        try:
            vectors = self.embedding_provider.embed_documents(
                [projection.text for projection in manifest.projections]
            )
            points = []
            for projection, vector in zip(
                manifest.projections, vectors, strict=True
            ):
                payload = projection.to_payload(
                    corpus_id=manifest.corpus_id,
                    manifest_id=manifest.id,
                    manifest_hash=manifest.manifest_hash,
                )
                # Staging points stay invisible to mandatory retrieval filters
                # until the complete manifest is present and verified.
                payload["review_status"] = ReviewStatus.APPROVED.value
                points.append(
                    models.PointStruct(
                        id=projection.knowledge_unit_id,
                        vector=vector,
                        payload=payload,
                    )
                )
            self.client.upsert(
                collection_name=self.collection_name,
                points=points,
                wait=True,
            )
            self._verify_written_points(
                manifest,
                expected_status=ReviewStatus.APPROVED,
            )
            self.client.set_payload(
                collection_name=self.collection_name,
                payload={"review_status": ReviewStatus.INGESTED.value},
                points=manifest.expected_point_ids,
                wait=True,
            )
            self._verify_written_points(
                manifest,
                expected_status=ReviewStatus.INGESTED,
            )
        except QdrantServiceError:
            raise
        except Exception as exc:
            raise QdrantServiceError(f"could not write authorized manifest: {exc}") from exc
        return self._receipt_for_manifest(manifest, operator)

    def _receipt_for_manifest(
        self,
        manifest: StorageManifest,
        actor: str,
    ) -> IngestionReceipt:
        return IngestionReceipt(
            manifest_id=manifest.id,
            manifest_hash=manifest.manifest_hash,
            corpus_id=manifest.corpus_id,
            collection_name=self.collection_name,
            point_ids=manifest.expected_point_ids,
            payload_checksums={
                projection.knowledge_unit_id: projection.payload_checksum
                for projection in manifest.projections
            },
            actor=actor,
        )

    def cleanup_manifest_points(self, manifest: StorageManifest) -> str:
        """Remove only points whose immutable ownership matches ``manifest``.

        The return value is persisted by the application saga.  Any ambiguity
        fails closed as ``reconciliation_required`` rather than deleting data
        that may belong to another corpus.
        """

        self._require_production_write_capability()
        self._assert_production_collection()
        try:
            if not self.client.collection_exists(self.collection_name):
                return "not_needed"
            records = self.client.retrieve(
                collection_name=self.collection_name,
                ids=manifest.expected_point_ids,
                with_payload=True,
                with_vectors=False,
            )
            expected = {
                projection.knowledge_unit_id: projection.payload_checksum
                for projection in manifest.projections
            }
            owned: list[str] = []
            for record in records:
                payload = record.payload or {}
                point_id = str(record.id)
                if point_id not in expected:
                    return "reconciliation_required"
                try:
                    status = ReviewStatus(str(payload.get("review_status") or ""))
                    if status not in {ReviewStatus.APPROVED, ReviewStatus.INGESTED}:
                        return "reconciliation_required"
                    self._validate_point_payload(
                        point_id=point_id,
                        payload=payload,
                        expected_checksum=expected[point_id],
                        identity={
                            "corpus_id": manifest.corpus_id,
                            "manifest_id": manifest.id,
                            "manifest_hash": manifest.manifest_hash,
                            "projection_schema": manifest.projection_schema_version,
                        },
                        mode=MagicForgeMode.PRODUCTION,
                        expected_status=status,
                    )
                except (QdrantServiceError, ValueError):
                    return "reconciliation_required"
                owned.append(point_id)
            if owned:
                self.client.delete(
                    collection_name=self.collection_name,
                    points_selector=models.PointIdsList(points=owned),
                    wait=True,
                )
            remaining = self.client.retrieve(
                collection_name=self.collection_name,
                ids=manifest.expected_point_ids,
                with_payload=False,
                with_vectors=False,
            )
            return "cleaned" if not remaining else "reconciliation_required"
        except Exception:
            return "reconciliation_required"

    def _verify_written_points(
        self,
        manifest: StorageManifest,
        *,
        expected_status: ReviewStatus,
    ) -> None:
        records = self.client.retrieve(
            collection_name=self.collection_name,
            ids=manifest.expected_point_ids,
            with_payload=True,
            with_vectors=False,
        )
        actual_ids = {str(record.id) for record in records}
        if actual_ids != set(manifest.expected_point_ids):
            raise QdrantServiceError(
                f"Qdrant verification returned {len(actual_ids)} of "
                f"{len(manifest.expected_point_ids)} points"
            )
        total_count = self.client.count(
            collection_name=self.collection_name,
            exact=True,
        ).count
        if total_count != manifest.expected_point_count:
            raise QdrantServiceError(
                "collection contains points outside the authorized manifest"
            )
        expected_checksums = {
            projection.knowledge_unit_id: projection.payload_checksum
            for projection in manifest.projections
        }
        for record in records:
            payload = record.payload or {}
            point_id = str(record.id)
            self._validate_point_payload(
                point_id=point_id,
                payload=payload,
                expected_checksum=expected_checksums[point_id],
                identity={
                    "corpus_id": manifest.corpus_id,
                    "manifest_id": manifest.id,
                    "manifest_hash": manifest.manifest_hash,
                    "projection_schema": manifest.projection_schema_version,
                },
                mode=MagicForgeMode.PRODUCTION,
                expected_status=expected_status,
            )

    def search_documents(
        self,
        query: str,
        limit: int = 5,
        filters: KnowledgeSearchFilter | None = None,
        *,
        authorization: RetrievalAuthorization | None = None,
    ) -> list[SearchResult]:
        auth = require_retrieval_authorization(authorization)
        if auth.bootstrap_limited and self.mode != MagicForgeMode.BOOTSTRAP:
            raise QdrantServiceError(
                "Bootstrap anonymous authorization cannot read Production knowledge"
            )
        if not query.strip():
            return []
        identity = self._active_search_identity()
        expected_checksums = self._active_payload_checksums()
        try:
            if not self.client.collection_exists(self.collection_name):
                return []
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=list(self._embed_query_cached(query)),
                query_filter=self._build_filter(
                    filters,
                    auth,
                    mode=self.mode,
                    corpus_id=identity["corpus_id"],
                    manifest_id=identity["manifest_id"],
                    manifest_hash=identity["manifest_hash"],
                    projection_schema=identity["projection_schema"],
                ),
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            results: list[SearchResult] = []
            for point in response.points:
                payload = point.payload or {}
                point_id = str(point.id)
                expected_checksum = expected_checksums.get(point_id)
                if expected_checksum is None:
                    raise QdrantServiceError(
                        "Qdrant returned a point outside the active manifest"
                    )
                self._validate_point_payload(
                    point_id=point_id,
                    payload=payload,
                    expected_checksum=expected_checksum,
                    identity=identity,
                    mode=self.mode,
                    expected_status=(
                        ReviewStatus.BOOTSTRAP
                        if self.mode == MagicForgeMode.BOOTSTRAP
                        else ReviewStatus.INGESTED
                    ),
                )
                if not self._payload_visible_to_authorization(payload, auth):
                    raise QdrantServiceError(
                        "Qdrant returned a point outside caller authorization"
                    )
                if filters is not None and not self._payload_matches_filters(
                    payload, filters
                ):
                    raise QdrantServiceError(
                        "Qdrant returned a point outside caller filters"
                    )
                results.append(
                    SearchResult(
                        text=str(payload.get("text", "")),
                        score=float(point.score),
                        payload=payload,
                    )
                )
            return results
        except QdrantServiceError:
            raise
        except Exception as exc:
            raise QdrantServiceError(f"could not search Qdrant: {exc}") from exc

    @lru_cache(maxsize=128)
    def _embed_query_cached(self, query: str) -> tuple[float, ...]:
        """Reuse an immutable query vector across routed retrieval channels."""

        return tuple(self.embedding_provider.embed_query(query))

    @staticmethod
    def _readiness_filter(
        *,
        corpus_id: str = "",
        manifest_id: str,
        manifest_hash: str,
        projection_schema: str,
        mode: MagicForgeMode,
    ) -> models.Filter:
        must: list[models.FieldCondition] = [
            models.FieldCondition(
                key="storage_manifest_id",
                match=models.MatchValue(value=manifest_id),
            ),
            models.FieldCondition(
                key="manifest_hash",
                match=models.MatchValue(value=manifest_hash),
            ),
            models.FieldCondition(
                key="schema_version",
                match=models.MatchValue(value=projection_schema),
            ),
        ]
        if mode == MagicForgeMode.BOOTSTRAP:
            must.extend(
                (
                    models.FieldCondition(
                        key="bootstrap_generated",
                        match=models.MatchValue(value=True),
                    ),
                    models.FieldCondition(
                        key="human_verified",
                        match=models.MatchValue(value=False),
                    ),
                    models.FieldCondition(
                        key="review_status",
                        match=models.MatchValue(value=ReviewStatus.BOOTSTRAP.value),
                    ),
                )
            )
        else:
            if not corpus_id.strip():
                raise QdrantServiceError(
                    "Production readiness requires an active corpus identity"
                )
            must.extend(
                (
                    models.FieldCondition(
                        key="corpus_id",
                        match=models.MatchValue(value=corpus_id.strip()),
                    ),
                    models.FieldCondition(
                        key="approved",
                        match=models.MatchValue(value=True),
                    ),
                    models.FieldCondition(
                        key="claim_eligibility",
                        match=models.MatchValue(value=True),
                    ),
                    models.FieldCondition(
                        key="storage_permission",
                        match=models.MatchValue(value=True),
                    ),
                    models.FieldCondition(
                        key="review_status",
                        match=models.MatchValue(value=ReviewStatus.INGESTED.value),
                    ),
                    models.FieldCondition(
                        key="bootstrap_generated",
                        match=models.MatchValue(value=False),
                    ),
                    models.FieldCondition(
                        key="human_verified",
                        match=models.MatchValue(value=True),
                    ),
                )
            )
        return models.Filter(must=must)

    @staticmethod
    def _build_filter(
        filters: KnowledgeSearchFilter | None,
        authorization: RetrievalAuthorization | None,
        *,
        mode: MagicForgeMode = MagicForgeMode.PRODUCTION,
        corpus_id: str = "",
        manifest_id: str = "",
        manifest_hash: str = "",
        projection_schema: str = "",
    ) -> models.Filter:
        auth = require_retrieval_authorization(authorization)
        if auth.bootstrap_limited and mode != MagicForgeMode.BOOTSTRAP:
            raise QdrantServiceError(
                "Bootstrap anonymous authorization cannot read Production knowledge"
            )
        if mode == MagicForgeMode.BOOTSTRAP:
            must: list[models.FieldCondition] = [
                models.FieldCondition(
                    key="bootstrap_generated", match=models.MatchValue(value=True)
                ),
                models.FieldCondition(
                    key="human_verified", match=models.MatchValue(value=False)
                ),
                models.FieldCondition(
                    key="review_status",
                    match=models.MatchValue(value=ReviewStatus.BOOTSTRAP.value),
                ),
            ]
        else:
            identity = {
                "corpus_id": corpus_id.strip(),
                "storage_manifest_id": manifest_id.strip(),
                "manifest_hash": manifest_hash.strip(),
                "schema_version": projection_schema.strip(),
            }
            if any(not value for value in identity.values()):
                raise QdrantServiceError(
                    "Production retrieval requires an active corpus and manifest identity"
                )
            must = [
                *(
                    models.FieldCondition(
                        key=key,
                        match=models.MatchValue(value=value),
                    )
                    for key, value in identity.items()
                ),
                models.FieldCondition(
                    key="approved", match=models.MatchValue(value=True)
                ),
                models.FieldCondition(
                    key="claim_eligibility", match=models.MatchValue(value=True)
                ),
                models.FieldCondition(
                    key="storage_permission", match=models.MatchValue(value=True)
                ),
                models.FieldCondition(
                    key="review_status",
                    match=models.MatchValue(value=ReviewStatus.INGESTED.value),
                ),
                models.FieldCondition(
                    key="bootstrap_generated", match=models.MatchValue(value=False)
                ),
                models.FieldCondition(
                    key="human_verified", match=models.MatchValue(value=True)
                ),
            ]
        must.extend(
            [
            models.FieldCondition(
                key="sensitive_information_level",
                match=models.MatchAny(
                    any=[level.value for level in auth.allowed_sensitive_levels]
                ),
            ),
            models.FieldCondition(
                key="secret_exposure_rank",
                range=models.Range(lte=int(auth.clearance_rank)),
            ),
            ]
        )
        if filters is not None:
            values_by_field = {
                "knowledge_type": filters.knowledge_types,
                "domain": filters.domains,
                "ontology_paths": filters.ontology_paths,
                "knowledge_origin": filters.knowledge_origins,
                "evidence_level": filters.evidence_levels,
                "entity_ids": filters.entity_ids,
                "entity_types": filters.entity_types,
                "relation_types": filters.relation_types,
            }
            must.extend(
                models.FieldCondition(
                    key=field_name,
                    match=models.MatchAny(any=values),
                )
                for field_name, values in values_by_field.items()
                if values
            )
        return models.Filter(
            must=must,
            must_not=[
                models.FieldCondition(
                    key="confidence_label",
                    match=models.MatchValue(value="insufficient"),
                )
            ],
        )

    @staticmethod
    def _normalize_expected_points(
        expected_point_count: int,
        expected_point_ids: Sequence[str],
        expected_payload_checksums: Mapping[str, str],
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        point_ids = tuple(str(value) for value in expected_point_ids)
        checksums = {
            str(point_id): str(checksum)
            for point_id, checksum in expected_payload_checksums.items()
        }
        if (
            len(point_ids) != expected_point_count
            or len(set(point_ids)) != expected_point_count
            or set(checksums) != set(point_ids)
            or any(len(checksum) != 64 for checksum in checksums.values())
        ):
            raise QdrantServiceError(
                "active manifest point identities and checksums are inconsistent"
            )
        return point_ids, checksums

    def _verify_active_points(
        self,
        *,
        expected_ids: Sequence[str],
        expected_checksums: Mapping[str, str],
        identity: Mapping[str, str],
        mode: MagicForgeMode,
    ) -> None:
        records = self.client.retrieve(
            collection_name=self.collection_name,
            ids=list(expected_ids),
            with_payload=True,
            with_vectors=False,
        )
        actual_ids = {str(record.id) for record in records}
        if actual_ids != set(expected_ids):
            raise QdrantServiceError(
                "Qdrant point identities do not match the active manifest"
            )
        expected_status = (
            ReviewStatus.BOOTSTRAP
            if mode == MagicForgeMode.BOOTSTRAP
            else ReviewStatus.INGESTED
        )
        for record in records:
            point_id = str(record.id)
            self._validate_point_payload(
                point_id=point_id,
                payload=record.payload or {},
                expected_checksum=expected_checksums[point_id],
                identity=identity,
                mode=mode,
                expected_status=expected_status,
            )

    @staticmethod
    def _canonical_projection_checksum(
        payload: Mapping[str, object],
        mode: MagicForgeMode,
    ) -> str:
        canonical = dict(payload)
        for key in (
            "storage_manifest_id",
            "manifest_hash",
            "payload_checksum",
        ):
            canonical.pop(key, None)
        try:
            if mode == MagicForgeMode.PRODUCTION:
                for key in ("corpus_id", "bootstrap_generated", "human_verified"):
                    canonical.pop(key, None)
                # ``approved`` is the only staging state.  It is not part of the
                # immutable projection and is normalized before hashing.
                canonical["review_status"] = ReviewStatus.INGESTED.value
                projection = QdrantProjection.model_validate(canonical)
            else:
                projection = BootstrapQdrantProjection.model_validate(canonical)
        except Exception as exc:
            raise QdrantServiceError(
                "Qdrant payload does not satisfy the active projection schema"
            ) from exc
        return projection.payload_checksum

    def _validate_point_payload(
        self,
        *,
        point_id: str,
        payload: Mapping[str, object],
        expected_checksum: str,
        identity: Mapping[str, str],
        mode: MagicForgeMode,
        expected_status: ReviewStatus,
    ) -> None:
        if (
            payload.get("knowledge_unit_id") != point_id
            or payload.get("payload_checksum") != expected_checksum
            or payload.get("storage_manifest_id") != identity["manifest_id"]
            or payload.get("manifest_hash") != identity["manifest_hash"]
            or payload.get("schema_version") != identity["projection_schema"]
            or payload.get("review_status") != expected_status.value
        ):
            raise QdrantServiceError(
                f"point {point_id} does not match the active manifest identity"
            )
        if mode == MagicForgeMode.PRODUCTION:
            if (
                payload.get("corpus_id") != identity["corpus_id"]
                or payload.get("bootstrap_generated") is not False
                or payload.get("human_verified") is not True
                or payload.get("approved") is not True
                or payload.get("claim_eligibility") is not True
                or payload.get("storage_permission") is not True
            ):
                raise QdrantServiceError(
                    f"point {point_id} does not satisfy Production governance"
                )
        elif (
            payload.get("bootstrap_generated") is not True
            or payload.get("human_verified") is not False
            or payload.get("approved") is not False
            or payload.get("claim_eligibility") is not False
            or payload.get("storage_permission") is not False
        ):
            raise QdrantServiceError(
                f"point {point_id} does not satisfy Bootstrap isolation"
            )
        if self._canonical_projection_checksum(payload, mode) != expected_checksum:
            raise QdrantServiceError(
                f"payload checksum mismatch for point {point_id}"
            )

    @staticmethod
    def _payload_visible_to_authorization(
        payload: Mapping[str, object],
        authorization: RetrievalAuthorization,
    ) -> bool:
        try:
            exposure_rank = int(payload.get("secret_exposure_rank", 99))
        except (TypeError, ValueError):
            return False
        return bool(
            payload.get("sensitive_information_level")
            in {level.value for level in authorization.allowed_sensitive_levels}
            and exposure_rank <= int(authorization.clearance_rank)
            and payload.get("confidence_label") != "insufficient"
        )

    @staticmethod
    def _payload_matches_filters(
        payload: Mapping[str, object],
        filters: KnowledgeSearchFilter,
    ) -> bool:
        values_by_field = {
            "knowledge_type": filters.knowledge_types,
            "domain": filters.domains,
            "ontology_paths": filters.ontology_paths,
            "knowledge_origin": filters.knowledge_origins,
            "evidence_level": filters.evidence_levels,
            "entity_ids": filters.entity_ids,
            "entity_types": filters.entity_types,
            "relation_types": filters.relation_types,
        }
        for field_name, expected in values_by_field.items():
            if not expected:
                continue
            value = payload.get(field_name)
            actual = set(value if isinstance(value, list) else [value])
            if not actual.intersection(expected):
                return False
        return True

    def _active_payload_checksums(self) -> dict[str, str]:
        if not self.active_payload_checksums:
            raise QdrantServiceError(
                "active manifest point identities and checksums are required"
            )
        if any(len(checksum) != 64 for checksum in self.active_payload_checksums.values()):
            raise QdrantServiceError("active manifest contains an invalid checksum")
        return dict(self.active_payload_checksums)

    def _active_search_identity(self) -> dict[str, str]:
        if self.mode == MagicForgeMode.BOOTSTRAP:
            identity = {
                "corpus_id": "",
                "manifest_id": self.active_manifest_id,
                "manifest_hash": self.active_manifest_hash,
                "projection_schema": self.active_projection_schema,
            }
            required = {key: value for key, value in identity.items() if key != "corpus_id"}
        else:
            identity = {
                "corpus_id": self.active_corpus_id,
                "manifest_id": self.active_manifest_id,
                "manifest_hash": self.active_manifest_hash,
                "projection_schema": self.active_projection_schema,
            }
            required = identity
        if any(not value for value in required.values()):
            raise QdrantServiceError(
                "retrieval requires an active corpus manifest identity"
            )
        return identity

    def _assert_production_collection(self) -> None:
        normalized = self.collection_name.strip().casefold()
        if normalized.startswith("magicforge_bootstrap") or "bootstrap" in normalized:
            raise QdrantServiceError(
                "Production writes to Bootstrap collections are prohibited"
            )

    def _require_production_write_capability(self) -> None:
        if (
            self.mode == MagicForgeMode.PRODUCTION
            and not self.production_writes_enabled
        ):
            raise QdrantServiceError(
                "Production Qdrant mutation capability is disabled"
            )

    def _manifest_is_fully_ingested(self, manifest: StorageManifest) -> bool:
        try:
            if not self.client.collection_exists(self.collection_name):
                return False
            total = self.client.count(
                collection_name=self.collection_name,
                exact=True,
            ).count
            if total != manifest.expected_point_count:
                return False
            self._verify_written_points(
                manifest,
                expected_status=ReviewStatus.INGESTED,
            )
            return True
        except QdrantServiceError:
            return False
        except Exception:
            return False

    def _require_collection_bound_to_manifest(
        self,
        manifest: StorageManifest,
    ) -> None:
        total = self.client.count(
            collection_name=self.collection_name,
            exact=True,
        ).count
        if total == 0:
            return
        try:
            self._verify_written_points(
                manifest,
                expected_status=ReviewStatus.INGESTED,
            )
        except QdrantServiceError as exc:
            raise QdrantServiceError(
                "collection is not exclusively bound to the authorized manifest"
            ) from exc
