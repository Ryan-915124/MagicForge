"""Magic Knowledge Schema v0.3.

Qdrant stores serialized projections of these storage-neutral models. Stable
node/edge IDs and a graph transfer record keep ingestion independent from any
future graph database implementation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "3.0"


class EntityType(StrEnum):
    EFFECT = "effect"
    TECHNIQUE = "technique"
    METHOD = "method"
    PSYCHOLOGY_PRINCIPLE = "psychology_principle"
    PERFORMER = "performer"
    SOURCE = "source"
    COGNITIVE_MECHANISM = "cognitive_mechanism"
    RESEARCH_PAPER = "research_paper"


class RelationType(StrEnum):
    USES = "uses"
    INSPIRED_BY = "inspired_by"
    REQUIRES = "requires"
    EXPLAINS = "explains"
    PERFORMED_BY = "performed_by"
    RELATED_TO = "related_to"

    @classmethod
    def _missing_(cls, value: object) -> "RelationType | None":
        """Normalize v0.1 relationship names when old documents are reingested."""

        aliases = {
            "uses_technique": "uses",
            "uses_method": "uses",
            "applies_principle": "uses",
            "documented_in": "explains",
            "created_by": "related_to",
            "variation_of": "inspired_by",
        }
        normalized = aliases.get(str(value).strip().casefold())
        return cls(normalized) if normalized else None


def stable_entity_id(entity_type: EntityType, name: str) -> str:
    """Return a deterministic UUID for an entity's normalized identity."""

    normalized_name = " ".join(name.casefold().split())
    return str(uuid5(NAMESPACE_URL, f"magicforge:{entity_type.value}:{normalized_name}"))


class KnowledgeEntity(BaseModel):
    """A node-shaped domain object, independent of any graph implementation."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    type: EntityType
    name: str = Field(min_length=1)
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return " ".join(value.split())

    @model_validator(mode="after")
    def assign_stable_id(self) -> "KnowledgeEntity":
        if not self.id:
            self.id = stable_entity_id(self.type, self.name)
        return self


class Effect(KnowledgeEntity):
    type: Literal[EntityType.EFFECT] = EntityType.EFFECT


class Technique(KnowledgeEntity):
    type: Literal[EntityType.TECHNIQUE] = EntityType.TECHNIQUE


class Method(KnowledgeEntity):
    type: Literal[EntityType.METHOD] = EntityType.METHOD


class PsychologyPrinciple(KnowledgeEntity):
    type: Literal[EntityType.PSYCHOLOGY_PRINCIPLE] = EntityType.PSYCHOLOGY_PRINCIPLE


class Performer(KnowledgeEntity):
    type: Literal[EntityType.PERFORMER] = EntityType.PERFORMER


class Source(KnowledgeEntity):
    type: Literal[EntityType.SOURCE] = EntityType.SOURCE


class CognitiveMechanism(KnowledgeEntity):
    type: Literal[EntityType.COGNITIVE_MECHANISM] = EntityType.COGNITIVE_MECHANISM


class ResearchPaper(KnowledgeEntity):
    type: Literal[EntityType.RESEARCH_PAPER] = EntityType.RESEARCH_PAPER


class KnowledgeRelationship(BaseModel):
    """A directed, evidence-bearing edge between two canonical entities."""

    model_config = ConfigDict(extra="forbid")

    id: str = ""
    source_id: str
    target_id: str
    type: RelationType
    evidence: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source_chunk_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def assign_stable_id(self) -> "KnowledgeRelationship":
        if not self.id:
            identity = ":".join(
                (
                    self.source_id,
                    self.type.value,
                    self.target_id,
                    self.evidence or "",
                )
            )
            self.id = str(uuid5(NAMESPACE_URL, f"magicforge:relation:{identity}"))
        return self


class KnowledgeMetadata(BaseModel):
    """Legacy parsing metadata; it is not a production storage authorization."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    document_id: str = Field(default_factory=lambda: str(uuid4()))
    source_id: str | None = None
    title: str = ""
    author: str = ""
    category: str = ""
    technique: list[str] = Field(default_factory=list)
    psychology: list[str] = Field(default_factory=list)
    performer: list[str] = Field(default_factory=list)
    entities: list[KnowledgeEntity] = Field(default_factory=list)
    relationships: list[KnowledgeRelationship] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("technique", "psychology", "performer", "tags", mode="before")
    @classmethod
    def coerce_string_list(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return [str(item).strip() for item in value if str(item).strip()]

    @model_validator(mode="after")
    def validate_references(self) -> "KnowledgeMetadata":
        entity_ids = {entity.id for entity in self.entities}
        if self.source_id and self.source_id not in entity_ids:
            raise ValueError("source_id must refer to an entity in entities")
        for relationship in self.relationships:
            missing = {
                endpoint
                for endpoint in (relationship.source_id, relationship.target_id)
                if endpoint not in entity_ids
            }
            if missing:
                raise ValueError(
                    "relationship endpoints must refer to entities in the same metadata: "
                    + ", ".join(sorted(missing))
                )
        return self

    def to_payload(self) -> dict[str, Any]:
        """Serialize the compatibility shape used by parsing/migration tests."""

        payload = self.model_dump(mode="json")
        payload["entity_ids"] = [entity.id for entity in self.entities]
        payload["entity_types"] = sorted({entity.type.value for entity in self.entities})
        payload["relation_types"] = sorted(
            {relationship.type.value for relationship in self.relationships}
        )
        return payload


class SourceReference(BaseModel):
    """A chunk-level citation candidate with a human-readable locator."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    author: str = ""
    locator: str | None = None
    source_id: str | None = None


class ChunkMetadata(BaseModel):
    """Structured metadata identified for one chunk, not the whole document."""

    model_config = ConfigDict(extra="forbid")

    magic_category: str = ""
    techniques: list[str] = Field(default_factory=list)
    psychological_principles: list[str] = Field(default_factory=list)
    performers: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    extraction_method: Literal["declared", "glm", "merged"] = "declared"
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator(
        "techniques", "psychological_principles", "performers", mode="before"
    )
    @classmethod
    def coerce_names(cls, value: Any) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return [str(item).strip() for item in value if str(item).strip()]


class KnowledgeChunk(BaseModel):
    """Legacy document-processing chunk; prohibited at the vector write boundary."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    text: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    heading: str | None = None
    page_number: int | None = Field(default=None, ge=1)
    source_locator: str | None = None
    annotations: ChunkMetadata = Field(default_factory=ChunkMetadata)
    metadata: KnowledgeMetadata

    @field_validator("id")
    @classmethod
    def qdrant_compatible_id(cls, value: str) -> str:
        # Qdrant point IDs accept UUID strings. Fail early with a useful error.
        UUID(value)
        return value

    def to_payload(self) -> dict[str, Any]:
        """Return a legacy payload shape; production Qdrant writers reject it."""

        payload = {
            "text": self.text,
            "chunk_id": self.id,
            "chunk_index": self.chunk_index,
            "heading": self.heading,
            "page_number": self.page_number,
            "source_locator": self.source_locator,
            "chunk_metadata": self.annotations.model_dump(mode="json"),
            "metadata_extraction": self.annotations.extraction_method,
            "metadata_confidence": self.annotations.confidence,
            **self.metadata.to_payload(),
        }
        # v0.1 filter keys now reflect chunk-specific extraction when present.
        payload["category"] = self.annotations.magic_category or self.metadata.category
        payload["technique"] = _unique(
            [*self.metadata.technique, *self.annotations.techniques]
        )
        payload["psychology"] = _unique(
            [*self.metadata.psychology, *self.annotations.psychological_principles]
        )
        payload["performer"] = _unique(
            [*self.metadata.performer, *self.annotations.performers]
        )
        payload["source_titles"] = _unique(
            [reference.title for reference in self.annotations.sources]
        )
        payload["magic_category"] = payload["category"]
        payload["techniques"] = payload["technique"]
        payload["psychological_principles"] = payload["psychology"]
        payload["performers"] = payload["performer"]
        payload["sources"] = [
            reference.model_dump(mode="json") for reference in self.annotations.sources
        ]
        return payload


class KnowledgeGraphRecord(BaseModel):
    """Database-neutral batch that a future graph adapter can consume."""

    schema_version: str = SCHEMA_VERSION
    document_id: str
    chunk_id: str
    entities: list[KnowledgeEntity]
    relationships: list[KnowledgeRelationship]

    @classmethod
    def from_chunk(cls, chunk: KnowledgeChunk) -> "KnowledgeGraphRecord":
        return cls(
            document_id=chunk.metadata.document_id,
            chunk_id=chunk.id,
            entities=chunk.metadata.entities,
            relationships=chunk.metadata.relationships,
        )


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            output.append(normalized)
    return output
