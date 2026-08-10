"""Ports for future knowledge graph persistence.

No implementation is provided in v0.3. A graph adapter can implement this
protocol while the rest of the application keeps using the canonical models.
"""

from __future__ import annotations

from typing import Protocol, Sequence

from knowledge.models import KnowledgeEntity, KnowledgeGraphRecord, KnowledgeRelationship


class KnowledgeGraphRepository(Protocol):
    def upsert_record(self, record: KnowledgeGraphRecord) -> None: ...

    def upsert_entities(self, entities: Sequence[KnowledgeEntity]) -> None: ...

    def upsert_relationships(
        self, relationships: Sequence[KnowledgeRelationship]
    ) -> None: ...

    def get_neighbors(
        self, entity_id: str, relationship_types: Sequence[str] | None = None
    ) -> Sequence[KnowledgeEntity]: ...
