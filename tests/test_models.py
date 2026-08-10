from uuid import uuid4

import pytest
from pydantic import ValidationError

from knowledge.models import (
    ChunkMetadata,
    EntityType,
    Effect,
    KnowledgeChunk,
    KnowledgeEntity,
    KnowledgeGraphRecord,
    KnowledgeMetadata,
    KnowledgeRelationship,
    RelationType,
    SourceReference,
    Technique,
)


def test_metadata_serializes_legacy_and_graph_ready_fields() -> None:
    effect = Effect(name="Ambitious Card")
    technique = Technique(name="Double Lift")
    source = KnowledgeEntity(type=EntityType.SOURCE, name="Card Notes")
    relationship = KnowledgeRelationship(
        source_id=effect.id,
        target_id=technique.id,
        type=RelationType.USES,
        confidence=0.8,
    )
    metadata = KnowledgeMetadata(
        source_id=source.id,
        title="Card Notes",
        author="A. Magician",
        category="card magic",
        technique="Double Lift",
        psychology=["Time Misdirection"],
        performer="Example Performer",
        entities=[effect, technique, source],
        relationships=[relationship],
    )

    payload = metadata.to_payload()

    assert payload["title"] == "Card Notes"
    assert payload["technique"] == ["Double Lift"]
    assert payload["psychology"] == ["Time Misdirection"]
    assert payload["performer"] == ["Example Performer"]
    assert payload["entity_ids"] == [effect.id, technique.id, source.id]
    assert payload["entity_types"] == ["effect", "source", "technique"]
    assert payload["schema_version"] == "3.0"
    assert payload["relation_types"] == ["uses"]


def test_entity_and_relationship_ids_are_deterministic() -> None:
    first = KnowledgeEntity(type=EntityType.METHOD, name="  Secret   Reversal ")
    second = KnowledgeEntity(type=EntityType.METHOD, name="secret reversal")
    assert first.id == second.id

    relation_one = KnowledgeRelationship(
        source_id=first.id,
        target_id=second.id,
        type=RelationType.RELATED_TO,
    )
    relation_two = KnowledgeRelationship(
        source_id=first.id,
        target_id=second.id,
        type=RelationType.RELATED_TO,
    )
    assert relation_one.id == relation_two.id


def test_metadata_rejects_dangling_relationships() -> None:
    source = KnowledgeEntity(type=EntityType.SOURCE, name="Notes")
    with pytest.raises(ValidationError, match="relationship endpoints"):
        KnowledgeMetadata(
            source_id=source.id,
            entities=[source],
            relationships=[
                KnowledgeRelationship(
                    source_id=source.id,
                    target_id=str(uuid4()),
                    type=RelationType.RELATED_TO,
                )
            ],
        )


def test_chunk_payload_flattens_metadata_for_qdrant_filters() -> None:
    source = KnowledgeEntity(type=EntityType.SOURCE, name="Notes")
    chunk = KnowledgeChunk(
        text="A useful passage.",
        chunk_index=0,
        page_number=2,
        source_locator="page 2",
        annotations=ChunkMetadata(
            magic_category="mentalism",
            techniques=["Equivoque"],
            psychological_principles=["Choice Architecture"],
            performers=["Example Performer"],
            sources=[SourceReference(title="Notes", locator="page 2")],
            extraction_method="glm",
            confidence=0.9,
        ),
        metadata=KnowledgeMetadata(source_id=source.id, entities=[source]),
    )

    payload = chunk.to_payload()

    assert payload["text"] == "A useful passage."
    assert payload["chunk_id"] == chunk.id
    assert payload["source_id"] == source.id
    assert payload["category"] == "mentalism"
    assert payload["technique"] == ["Equivoque"]
    assert payload["psychology"] == ["Choice Architecture"]
    assert payload["performer"] == ["Example Performer"]
    assert payload["source_titles"] == ["Notes"]
    assert payload["magic_category"] == "mentalism"
    assert payload["techniques"] == ["Equivoque"]
    assert payload["psychological_principles"] == ["Choice Architecture"]
    assert payload["performers"] == ["Example Performer"]
    assert payload["sources"][0]["locator"] == "page 2"
    assert payload["page_number"] == 2

    graph_record = KnowledgeGraphRecord.from_chunk(chunk)
    assert graph_record.schema_version == "3.0"
    assert graph_record.chunk_id == chunk.id


def test_schema_v02_has_exact_relationship_vocabulary_and_legacy_aliases() -> None:
    assert {item.value for item in RelationType} == {
        "uses",
        "inspired_by",
        "requires",
        "explains",
        "performed_by",
        "related_to",
    }
    assert RelationType("uses_technique") == RelationType.USES
    assert RelationType("documented_in") == RelationType.EXPLAINS
