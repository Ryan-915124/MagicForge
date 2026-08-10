from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from qdrant_client import models

from knowledge.bootstrap import BootstrapQdrantProjection
from knowledge.governance import MagicForgeMode
from knowledge.models import EntityType, KnowledgeChunk, KnowledgeEntity, KnowledgeMetadata
from knowledge.projections import (
    ArtifactType,
    KnowledgeType,
    QdrantProjection,
    StorageManifestService,
)
from research.evidence import (
    ClaimRole,
    ConfidenceLabel,
    ContradictionStatus,
    EvidenceClass,
    EvidenceLevel,
    KnowledgeOrigin,
    MagicDomain,
    SourceType,
)
from research.governance import (
    ReviewStatus,
    SecretExposureLevel,
    SensitiveInformationLevel,
)
from retrieval.interfaces import (
    KnowledgeSearchFilter,
    RetrievalAuthorizationRequiredError,
)
from retrieval.qdrant_service import QdrantService, QdrantServiceError
from persistence.models import RoleName
from security.policy import AuthenticatedActor, retrieval_authorization_for_actor

TEST_CORPUS_ID = "a92067d2-128e-4a10-a7f5-5f99fc127b80"
TEST_MANIFEST_ID = "d037142a-ff81-48c9-9997-d4b19662a440"
TEST_MANIFEST_HASH = "a" * 64
TEST_PROJECTION_SCHEMA = "qdrant-0.2"
TEST_BOOTSTRAP_MANIFEST_ID = "6d31e8fc-4626-4e76-a21c-392ce41215a2"
TEST_BOOTSTRAP_MANIFEST_HASH = "b" * 64
TEST_BOOTSTRAP_PROJECTION_SCHEMA = "bootstrap-qdrant-0.2"


class FakeEmbeddings:
    dimension = 3

    def __init__(self) -> None:
        self.query_calls = []

    def embed_documents(self, texts):
        return [[float(index), 0.0, 1.0] for index, _ in enumerate(texts)]

    def embed_query(self, text):
        self.query_calls.append(text)
        return [0.0, 1.0, 0.0]


class FakeQdrantClient:
    def __init__(self) -> None:
        self.exists = False
        self.created = None
        self.indexes = []
        self.upserted = None
        self.query_kwargs = None
        self.vector_size = 3
        self.distance = models.Distance.COSINE
        self.total_count = 1
        self.filtered_count = 1
        self.count_calls = []
        self.close_calls = 0
        self.readiness_records = []
        self.query_point = None

    def collection_exists(self, collection_name):
        return self.exists

    def create_collection(self, **kwargs):
        self.created = kwargs
        self.exists = True

    def create_payload_index(self, **kwargs):
        self.indexes.append(kwargs["field_name"])

    def get_collection(self, collection_name):
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=SimpleNamespace(
                        size=self.vector_size,
                        distance=self.distance,
                    )
                )
            )
        )

    def count(self, *, count_filter=None, **kwargs):
        self.count_calls.append(
            {"count_filter": count_filter, **kwargs}
        )
        return SimpleNamespace(
            count=self.total_count if count_filter is None else self.filtered_count
        )

    def close(self):
        self.close_calls += 1

    def upsert(self, **kwargs):
        self.upserted = kwargs

    def set_payload(self, *, payload, points, **kwargs):
        by_id = {str(point.id): point for point in self.upserted["points"]}
        for point_id in points:
            by_id[str(point_id)].payload.update(payload)

    def retrieve(self, *, ids, **kwargs):
        if self.upserted is None:
            wanted = {str(value) for value in ids}
            return [
                record
                for record in self.readiness_records
                if str(record.id) in wanted
            ]
        by_id = {str(point.id): point for point in self.upserted["points"]}
        return [
            SimpleNamespace(id=value, payload=by_id[str(value)].payload)
            for value in ids
        ]

    def query_points(self, **kwargs):
        self.query_kwargs = kwargs
        return SimpleNamespace(points=[self.query_point] if self.query_point else [])


class MissingPointQdrantClient(FakeQdrantClient):
    def __init__(self) -> None:
        super().__init__()
        self.promoted = False

    def retrieve(self, *, ids, **kwargs):
        return []

    def set_payload(self, *, payload, points, **kwargs):
        self.promoted = True


def _reader_authorization():
    return retrieval_authorization_for_actor(
        AuthenticatedActor(
            user_id=uuid4(),
            username="reader",
            roles=frozenset({RoleName.READER}),
        )
    )


def _projection() -> QdrantProjection:
    artifact_id = str(uuid4())
    card_id = artifact_id
    source_id = str(uuid4())
    return QdrantProjection(
        knowledge_unit_id=QdrantProjection.id_for_artifact(artifact_id, 1),
        artifact_type=ArtifactType.EVIDENCE_CARD,
        artifact_id=artifact_id,
        artifact_version=1,
        knowledge_type=KnowledgeType.EVIDENCE,
        text=(
            "Knowledge type: evidence\n"
            "Claim: Attention may limit reporting.\n"
            "Limitations: Context dependent."
        ),
        title="Attention may limit reporting",
        domain=[MagicDomain.THEORY],
        ontology_paths=["psychology.attention"],
        knowledge_origin=KnowledgeOrigin.SCIENTIFIC_EVIDENCE,
        evidence_level=EvidenceLevel.REVIEW,
        evidence_class=EvidenceClass.SYSTEMATIC_REVIEW,
        claim_roles=[ClaimRole.RESULT],
        confidence=0.8,
        confidence_label=ConfidenceLabel.HIGH,
        limitations=["Context dependent."],
        source_type=SourceType.JOURNAL_ARTICLE,
        source_id=source_id,
        citation_id=str(uuid4()),
        source_candidate_id=str(uuid4()),
        document_id=str(uuid4()),
        source_locator="page 7",
        page_number=7,
        source_year=2020,
        evidence_card_id=card_id,
        canonical_claim_id=str(uuid4()),
        supporting_evidence_ids=[card_id],
        contradiction_status=ContradictionStatus.NONE_FOUND,
        entity_ids=[source_id],
        entity_types=[EntityType.SOURCE],
        secret_exposure_level=SecretExposureLevel.GENERAL_PRINCIPLE,
        secret_exposure_rank=1,
        sensitive_information_level=SensitiveInformationLevel.CONTROLLED,
        review_status=ReviewStatus.INGESTED,
        review_item_id=str(uuid4()),
        claim_review_item_ids=[str(uuid4())],
        reviewed_at=datetime.now(UTC),
    )


def _bootstrap_projection() -> BootstrapQdrantProjection:
    production = _projection()
    payload = production.model_dump(mode="json")
    for key in (
        "review_item_id",
        "claim_review_item_ids",
        "reviewed_at",
    ):
        payload.pop(key)
    payload.update(
        {
            "schema_version": TEST_BOOTSTRAP_PROJECTION_SCHEMA,
            "knowledge_unit_id": BootstrapQdrantProjection.id_for_artifact(
                production.artifact_id,
                production.artifact_version,
            ),
            "source_review_status": "bootstrap_pending_human_review",
            "review_status": "bootstrap",
            "verification_status": "unverified",
            "bootstrap_generated": True,
            "human_verified": False,
            "approved": False,
            "claim_eligibility": False,
            "storage_permission": False,
            "created_at": datetime.now(UTC).isoformat(),
        }
    )
    return BootstrapQdrantProjection.model_validate(payload)


def _production_payload(projection: QdrantProjection) -> dict[str, object]:
    return projection.to_payload(
        corpus_id=TEST_CORPUS_ID,
        manifest_id=TEST_MANIFEST_ID,
        manifest_hash=TEST_MANIFEST_HASH,
    )


def _configure_production_query(
    client: FakeQdrantClient,
    projection: QdrantProjection,
) -> None:
    client.query_point = SimpleNamespace(
        id=projection.knowledge_unit_id,
        score=0.91,
        payload=_production_payload(projection),
    )


def _legacy_chunk() -> KnowledgeChunk:
    source = KnowledgeEntity(type=EntityType.SOURCE, name="Notes")
    return KnowledgeChunk(
        text="Stored passage",
        chunk_index=0,
        metadata=KnowledgeMetadata(
            source_id=source.id,
            title="Notes",
            entities=[source],
        ),
    )


def test_raw_knowledge_chunks_are_rejected() -> None:
    service = QdrantService(
        "http://unused", "magicforge_knowledge_v01", FakeEmbeddings(), FakeQdrantClient()
    )

    with pytest.raises(QdrantServiceError, match="raw document/chunk"):
        service.add_documents([_legacy_chunk()])


def test_bootstrap_readiness_validates_collection_identity_and_safety() -> None:
    client = FakeQdrantClient()
    client.exists = True
    projection = _bootstrap_projection()
    payload = projection.to_payload(
        manifest_id=TEST_BOOTSTRAP_MANIFEST_ID,
        manifest_hash=TEST_BOOTSTRAP_MANIFEST_HASH,
    )
    client.readiness_records = [
        SimpleNamespace(id=projection.knowledge_unit_id, payload=payload)
    ]
    service = QdrantService(
        "http://unused",
        "magicforge_bootstrap_v03",
        FakeEmbeddings(),
        client,
        mode=MagicForgeMode.BOOTSTRAP,
        active_manifest_id=TEST_BOOTSTRAP_MANIFEST_ID,
        active_manifest_hash=TEST_BOOTSTRAP_MANIFEST_HASH,
        active_projection_schema=TEST_BOOTSTRAP_PROJECTION_SCHEMA,
        active_payload_checksums={
            projection.knowledge_unit_id: projection.payload_checksum
        },
    )

    service.validate_readiness(
        expected_point_count=1,
        expected_point_ids=[projection.knowledge_unit_id],
        expected_payload_checksums={
            projection.knowledge_unit_id: projection.payload_checksum
        },
        manifest_id=TEST_BOOTSTRAP_MANIFEST_ID,
        manifest_hash=TEST_BOOTSTRAP_MANIFEST_HASH,
        expected_projection_schema=TEST_BOOTSTRAP_PROJECTION_SCHEMA,
    )

    assert len(client.count_calls) == 2
    assert client.count_calls[0]["count_filter"] is None
    filtered = client.count_calls[1]["count_filter"]
    values = {condition.key: condition.match.value for condition in filtered.must}
    assert values == {
        "storage_manifest_id": TEST_BOOTSTRAP_MANIFEST_ID,
        "manifest_hash": TEST_BOOTSTRAP_MANIFEST_HASH,
        "schema_version": TEST_BOOTSTRAP_PROJECTION_SCHEMA,
        "bootstrap_generated": True,
        "human_verified": False,
        "review_status": "bootstrap",
    }


def test_production_readiness_uses_approved_projection_markers() -> None:
    client = FakeQdrantClient()
    client.exists = True
    projection = _projection()
    client.readiness_records = [
        SimpleNamespace(
            id=projection.knowledge_unit_id,
            payload=_production_payload(projection),
        )
    ]
    service = QdrantService(
        "http://unused",
        "magicforge_knowledge_v01",
        FakeEmbeddings(),
        client,
        mode=MagicForgeMode.PRODUCTION,
        active_corpus_id=TEST_CORPUS_ID,
        active_manifest_id=TEST_MANIFEST_ID,
        active_manifest_hash=TEST_MANIFEST_HASH,
        active_projection_schema=TEST_PROJECTION_SCHEMA,
        active_payload_checksums={
            projection.knowledge_unit_id: projection.payload_checksum
        },
    )

    service.validate_readiness(
        expected_point_count=1,
        expected_point_ids=[projection.knowledge_unit_id],
        expected_payload_checksums={
            projection.knowledge_unit_id: projection.payload_checksum
        },
        corpus_id=TEST_CORPUS_ID,
        manifest_id=TEST_MANIFEST_ID,
        manifest_hash=TEST_MANIFEST_HASH,
        expected_projection_schema=TEST_PROJECTION_SCHEMA,
    )

    filtered = client.count_calls[1]["count_filter"]
    values = {condition.key: condition.match.value for condition in filtered.must}
    assert values == {
        "corpus_id": TEST_CORPUS_ID,
        "storage_manifest_id": TEST_MANIFEST_ID,
        "manifest_hash": TEST_MANIFEST_HASH,
        "schema_version": "qdrant-0.2",
        "approved": True,
        "claim_eligibility": True,
        "storage_permission": True,
        "review_status": "ingested",
        "bootstrap_generated": False,
        "human_verified": True,
    }


def test_readiness_rejects_a_missing_collection_without_creating_it() -> None:
    client = FakeQdrantClient()
    point_id = str(uuid4())
    service = QdrantService(
        "http://unused",
        "magicforge_bootstrap_v03",
        FakeEmbeddings(),
        client,
        mode=MagicForgeMode.BOOTSTRAP,
    )

    with pytest.raises(QdrantServiceError, match="is not available"):
        service.validate_readiness(
            expected_point_count=1,
            expected_point_ids=[point_id],
            expected_payload_checksums={point_id: "c" * 64},
            manifest_id=TEST_BOOTSTRAP_MANIFEST_ID,
            manifest_hash=TEST_BOOTSTRAP_MANIFEST_HASH,
            expected_projection_schema=TEST_BOOTSTRAP_PROJECTION_SCHEMA,
        )

    assert client.created is None
    assert client.count_calls == []


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("vector_size", 4, "vector size"),
        ("distance", models.Distance.DOT, "cosine distance"),
        ("total_count", 2, "active manifest expects"),
        ("filtered_count", 0, "active manifest and mode"),
    ],
)
def test_readiness_rejects_collection_or_identity_mismatches(
    attribute, value, message
) -> None:
    client = FakeQdrantClient()
    client.exists = True
    setattr(client, attribute, value)
    point_id = str(uuid4())
    service = QdrantService(
        "http://unused",
        "magicforge_bootstrap_v03",
        FakeEmbeddings(),
        client,
        mode=MagicForgeMode.BOOTSTRAP,
    )

    with pytest.raises(QdrantServiceError, match=message):
        service.validate_readiness(
            expected_point_count=1,
            expected_point_ids=[point_id],
            expected_payload_checksums={point_id: "c" * 64},
            manifest_id=TEST_BOOTSTRAP_MANIFEST_ID,
            manifest_hash=TEST_BOOTSTRAP_MANIFEST_HASH,
            expected_projection_schema=TEST_BOOTSTRAP_PROJECTION_SCHEMA,
        )


def test_close_is_idempotent_and_prevents_lazy_reopening() -> None:
    client = FakeQdrantClient()
    service = QdrantService(
        "http://unused",
        "magicforge_knowledge_v01",
        FakeEmbeddings(),
        client,
        production_writes_enabled=True,
    )
    service._embed_query_cached("cached query")

    service.close()
    service.close()

    assert client.close_calls == 1
    assert service._embed_query_cached.cache_info().currsize == 0
    with pytest.raises(QdrantServiceError, match="is closed"):
        _ = service.client


def test_authorized_manifest_creates_indexes_and_upserts_projection() -> None:
    client = FakeQdrantClient()
    service = QdrantService(
        "http://unused",
        "magicforge_knowledge_v01",
        FakeEmbeddings(),
        client,
        production_writes_enabled=True,
    )
    manifest_service = StorageManifestService()
    manifest = manifest_service.build(
        [_projection()],
        corpus_id=TEST_CORPUS_ID,
        validation_rule_versions={"projection_gate": "0.2"},
        collection_name="magicforge_knowledge_v01",
        created_by="research-pipeline",
    )
    manifest = manifest_service.authorize(
        manifest,
        authorizer="Ryan Chen",
        reason="Exact projection approved for storage.",
    )

    receipt = service.write_manifest(manifest, actor="Ryan Chen")

    assert receipt.point_ids == manifest.expected_point_ids
    assert client.created["collection_name"] == "magicforge_knowledge_v01"
    assert {
        "approved",
        "secret_exposure_rank",
        "evidence_card_id",
        "artifact_version",
        "storage_manifest_id",
        "manifest_hash",
    }.issubset(client.indexes)
    point = client.upserted["points"][0]
    assert point.payload["approved"] is True
    assert point.payload["storage_manifest_id"] == manifest.id
    assert "Attention may limit reporting" in point.payload["text"]
    assert "Stored passage" not in point.payload["text"]


def test_search_documents_always_applies_security_and_domain_filters() -> None:
    client = FakeQdrantClient()
    client.exists = True
    projection = _projection()
    _configure_production_query(client, projection)
    service = QdrantService(
        "http://unused",
        "magicforge_knowledge_v01",
        FakeEmbeddings(),
        client,
        active_corpus_id=TEST_CORPUS_ID,
        active_manifest_id=TEST_MANIFEST_ID,
        active_manifest_hash=TEST_MANIFEST_HASH,
        active_projection_schema=TEST_PROJECTION_SCHEMA,
        active_payload_checksums={
            projection.knowledge_unit_id: projection.payload_checksum
        },
    )

    results = service.search_documents(
        "query",
        filters=KnowledgeSearchFilter(
            entity_types=["source"], domains=["theory"]
        ),
        authorization=_reader_authorization(),
    )

    assert results[0].text.startswith("Knowledge type: evidence")
    assert results[0].score == 0.91
    query_filter = client.query_kwargs["query_filter"]
    must_keys = {condition.key for condition in query_filter.must}
    assert {
        "approved",
        "claim_eligibility",
        "storage_permission",
        "review_status",
        "sensitive_information_level",
        "secret_exposure_rank",
        "entity_types",
        "domain",
    }.issubset(must_keys)
    assert {condition.key for condition in query_filter.must_not} == {
        "confidence_label"
    }


def test_partial_manifest_write_remains_hidden_and_has_no_receipt() -> None:
    client = MissingPointQdrantClient()
    service = QdrantService(
        "http://unused",
        "magicforge_knowledge_v01",
        FakeEmbeddings(),
        client,
        production_writes_enabled=True,
    )
    manifests = StorageManifestService()
    manifest = manifests.authorize(
        manifests.build(
            [_projection()],
            corpus_id=TEST_CORPUS_ID,
            validation_rule_versions={"projection_gate": "0.2"},
            collection_name="magicforge_knowledge_v01",
            created_by="research-pipeline",
        ),
        authorizer="Ryan Chen",
        reason="Exact projection approved for storage.",
    )

    with pytest.raises(QdrantServiceError, match="verification returned 0 of 1"):
        service.write_manifest(manifest, actor="Ryan Chen")

    assert client.promoted is False
    assert client.upserted["points"][0].payload["review_status"] == "approved"


def test_search_without_authorization_fails_before_qdrant_query() -> None:
    client = FakeQdrantClient()
    client.exists = True
    service = QdrantService(
        "http://unused", "magicforge_knowledge_v01", FakeEmbeddings(), client
    )

    with pytest.raises(RetrievalAuthorizationRequiredError):
        service.search_documents("query")

    assert client.query_kwargs is None


def test_search_reuses_query_embedding_across_routed_filters() -> None:
    client = FakeQdrantClient()
    client.exists = True
    embeddings = FakeEmbeddings()
    projection = _projection()
    service = QdrantService(
        "http://unused",
        "magicforge_knowledge_v01",
        embeddings,
        client,
        active_corpus_id=TEST_CORPUS_ID,
        active_manifest_id=TEST_MANIFEST_ID,
        active_manifest_hash=TEST_MANIFEST_HASH,
        active_projection_schema=TEST_PROJECTION_SCHEMA,
        active_payload_checksums={
            projection.knowledge_unit_id: projection.payload_checksum
        },
    )

    service.search_documents(
        "same routed query",
        filters=KnowledgeSearchFilter(knowledge_types=["psychology"]),
        authorization=_reader_authorization(),
    )
    service.search_documents(
        "same routed query",
        filters=KnowledgeSearchFilter(knowledge_types=["technique"]),
        authorization=_reader_authorization(),
    )
    service.search_documents(
        "different query",
        authorization=_reader_authorization(),
    )

    assert embeddings.query_calls == ["same routed query", "different query"]


def test_production_mutation_capability_is_disabled_by_default() -> None:
    client = FakeQdrantClient()
    service = QdrantService(
        "http://unused", "magicforge_knowledge_v01", FakeEmbeddings(), client
    )
    manifests = StorageManifestService()
    manifest = manifests.authorize(
        manifests.build(
            [_projection()],
            corpus_id=TEST_CORPUS_ID,
            validation_rule_versions={"projection_gate": "0.2"},
            collection_name="magicforge_knowledge_v01",
            created_by="Ryan Chen",
        ),
        authorizer="Ryan Chen",
        reason="Exact projection approved for storage.",
    )

    with pytest.raises(QdrantServiceError, match="mutation capability is disabled"):
        service.write_manifest(manifest, actor="Ryan Chen")
    with pytest.raises(QdrantServiceError, match="mutation capability is disabled"):
        service.create_collection()

    assert client.created is None
    assert client.upserted is None


def test_search_recomputes_payload_checksum_before_returning_content() -> None:
    client = FakeQdrantClient()
    client.exists = True
    projection = _projection()
    payload = _production_payload(projection)
    payload["text"] = str(payload["text"]) + "\nTampered content"
    client.query_point = SimpleNamespace(
        id=projection.knowledge_unit_id,
        score=0.91,
        payload=payload,
    )
    service = QdrantService(
        "http://unused",
        "magicforge_knowledge_v01",
        FakeEmbeddings(),
        client,
        active_corpus_id=TEST_CORPUS_ID,
        active_manifest_id=TEST_MANIFEST_ID,
        active_manifest_hash=TEST_MANIFEST_HASH,
        active_projection_schema=TEST_PROJECTION_SCHEMA,
        active_payload_checksums={
            projection.knowledge_unit_id: projection.payload_checksum
        },
    )

    with pytest.raises(QdrantServiceError, match="payload checksum mismatch"):
        service.search_documents("query", authorization=_reader_authorization())


def test_search_locally_rejects_a_qdrant_result_above_actor_clearance() -> None:
    client = FakeQdrantClient()
    client.exists = True
    payload = _projection().model_dump(mode="json")
    payload.update(
        {
            "sensitive_information_level": "restricted",
            "secret_exposure_level": "operational_secret",
            "secret_exposure_rank": 3,
        }
    )
    projection = QdrantProjection.model_validate(payload)
    _configure_production_query(client, projection)
    service = QdrantService(
        "http://unused",
        "magicforge_knowledge_v01",
        FakeEmbeddings(),
        client,
        active_corpus_id=TEST_CORPUS_ID,
        active_manifest_id=TEST_MANIFEST_ID,
        active_manifest_hash=TEST_MANIFEST_HASH,
        active_projection_schema=TEST_PROJECTION_SCHEMA,
        active_payload_checksums={
            projection.knowledge_unit_id: projection.payload_checksum
        },
    )

    with pytest.raises(QdrantServiceError, match="outside caller authorization"):
        service.search_documents("query", authorization=_reader_authorization())


def test_search_locally_rejects_a_qdrant_result_outside_caller_filters() -> None:
    client = FakeQdrantClient()
    client.exists = True
    projection = _projection()
    _configure_production_query(client, projection)
    service = QdrantService(
        "http://unused",
        "magicforge_knowledge_v01",
        FakeEmbeddings(),
        client,
        active_corpus_id=TEST_CORPUS_ID,
        active_manifest_id=TEST_MANIFEST_ID,
        active_manifest_hash=TEST_MANIFEST_HASH,
        active_projection_schema=TEST_PROJECTION_SCHEMA,
        active_payload_checksums={
            projection.knowledge_unit_id: projection.payload_checksum
        },
    )

    with pytest.raises(QdrantServiceError, match="outside caller filters"):
        service.search_documents(
            "query",
            filters=KnowledgeSearchFilter(knowledge_types=["technique"]),
            authorization=_reader_authorization(),
        )
