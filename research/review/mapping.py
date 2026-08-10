"""Human domain review for entity and relationship mapping proposals."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from knowledge.models import KnowledgeRelationship
from knowledge.projections import (
    KnowledgeNodeVersion,
    KnowledgeRelationshipAssertion,
)
from knowledge.evidence import ConfidenceAssessment, EvidenceCard, KnowledgeOrigin, MagicDomain
from research.extraction.models import EntityMappingProposal, RelationshipMappingProposal
from knowledge.governance import ReviewEvent, ReviewStatus, require_human_identity


class MappingReviewError(ValueError):
    pass


class MappingReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    entity_proposal: EntityMappingProposal | None = None
    relationship_proposal: RelationshipMappingProposal | None = None
    status: ReviewStatus = ReviewStatus.PENDING
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    reason: str | None = None
    approved_node: KnowledgeNodeVersion | None = None
    approved_relationship: KnowledgeRelationshipAssertion | None = None
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    audit_log: list[ReviewEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def assign_id_and_validate(self) -> "MappingReviewItem":
        proposals = [
            proposal
            for proposal in (self.entity_proposal, self.relationship_proposal)
            if proposal is not None
        ]
        if len(proposals) != 1:
            raise ValueError("mapping review requires exactly one proposal")
        item_id = str(
            uuid5(NAMESPACE_URL, f"magicforge:mapping-review:{proposals[0].id}")
        )
        if self.id and self.id != item_id:
            raise ValueError("mapping review ID does not match proposal")
        object.__setattr__(self, "id", item_id)
        artifacts = [
            artifact
            for artifact in (self.approved_node, self.approved_relationship)
            if artifact is not None
        ]
        if self.status == ReviewStatus.APPROVED:
            if len(artifacts) != 1 or not self.reviewer or not self.reviewed_at or not self.reason:
                raise ValueError("approved mapping requires one artifact and human decision")
        elif artifacts:
            raise ValueError("unapproved mapping cannot contain approved artifacts")
        if self.status == ReviewStatus.INGESTED:
            raise ValueError("mapping review records do not own ingestion state")
        return self


class MappingReviewRepository(Protocol):
    def save(self, item: MappingReviewItem) -> None: ...

    def get(self, item_id: str) -> MappingReviewItem | None: ...

    def list(self, status: ReviewStatus | None = None) -> list[MappingReviewItem]: ...


class InMemoryMappingReviewRepository:
    def __init__(self) -> None:
        self._items: dict[str, MappingReviewItem] = {}

    def save(self, item: MappingReviewItem) -> None:
        self._items[item.id] = item.model_copy(deep=True)

    def get(self, item_id: str) -> MappingReviewItem | None:
        item = self._items.get(item_id)
        return item.model_copy(deep=True) if item else None

    def list(self, status: ReviewStatus | None = None) -> list[MappingReviewItem]:
        return [
            item.model_copy(deep=True)
            for item in sorted(self._items.values(), key=lambda value: value.submitted_at)
            if status is None or item.status == status
        ]


class JsonMappingReviewRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def save(self, item: MappingReviewItem) -> None:
        with self._lock:
            items = {current.id: current for current in self._read()}
            items[item.id] = item
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(
                    [value.model_dump(mode="json") for value in items.values()],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            temporary.replace(self.path)

    def get(self, item_id: str) -> MappingReviewItem | None:
        with self._lock:
            return next((item for item in self._read() if item.id == item_id), None)

    def list(self, status: ReviewStatus | None = None) -> list[MappingReviewItem]:
        with self._lock:
            return [
                item
                for item in sorted(self._read(), key=lambda value: value.submitted_at)
                if status is None or item.status == status
            ]

    def _read(self) -> list[MappingReviewItem]:
        if not self.path.exists():
            return []
        return TypeAdapter(list[MappingReviewItem]).validate_json(
            self.path.read_text(encoding="utf-8")
        )


class MappingReviewService:
    def __init__(self, repository: MappingReviewRepository) -> None:
        self.repository = repository

    def submit_entity(
        self,
        proposal: EntityMappingProposal,
        *,
        actor: str = "research-pipeline",
    ) -> MappingReviewItem:
        return self._submit(
            MappingReviewItem(
                entity_proposal=proposal,
                audit_log=[ReviewEvent(action="entity_mapping_submitted", actor=actor)],
            )
        )

    def submit_relationship(
        self,
        proposal: RelationshipMappingProposal,
        *,
        actor: str = "research-pipeline",
    ) -> MappingReviewItem:
        return self._submit(
            MappingReviewItem(
                relationship_proposal=proposal,
                audit_log=[
                    ReviewEvent(action="relationship_mapping_submitted", actor=actor)
                ],
            )
        )

    def approve_entity(
        self,
        item_id: str,
        *,
        cards: list[EvidenceCard],
        reviewer: str,
        reason: str,
        definition: str,
        domains: list[MagicDomain],
        ontology_paths: list[str],
        limitations: list[str],
        confidence: ConfidenceAssessment,
        knowledge_origin: KnowledgeOrigin,
        topic_tags: list[str] | None = None,
    ) -> MappingReviewItem:
        item = self._pending(item_id)
        if item.entity_proposal is None:
            raise MappingReviewError("mapping review item is not an entity proposal")
        human, evidence_cards = self._validate_decision(
            item.entity_proposal.supporting_evidence_card_ids,
            cards,
            reviewer,
            reason,
            confidence,
            knowledge_origin,
        )
        now = datetime.now(UTC)
        node = KnowledgeNodeVersion(
            entity=item.entity_proposal.entity,
            definition=definition,
            domains=domains,
            ontology_paths=ontology_paths,
            topic_tags=topic_tags or [],
            knowledge_origin=knowledge_origin,
            supporting_evidence_ids=[card.id for card in evidence_cards],
            contradicting_evidence_ids=_contradicting_ids(evidence_cards),
            limitations=limitations,
            confidence=confidence,
            review_item_id=item.id,
            reviewer=human,
            reviewed_at=now,
        )
        return self._approve(item, human, reason, now, approved_node=node)

    def approve_relationship(
        self,
        item_id: str,
        *,
        cards: list[EvidenceCard],
        reviewer: str,
        reason: str,
        assertion: str,
        limitations: list[str],
        confidence: ConfidenceAssessment,
        knowledge_origin: KnowledgeOrigin,
    ) -> MappingReviewItem:
        item = self._pending(item_id)
        if item.relationship_proposal is None:
            raise MappingReviewError("mapping review item is not a relationship proposal")
        human, evidence_cards = self._validate_decision(
            item.relationship_proposal.supporting_evidence_card_ids,
            cards,
            reviewer,
            reason,
            confidence,
            knowledge_origin,
        )
        now = datetime.now(UTC)
        relationship = KnowledgeRelationship(
            source_id=item.relationship_proposal.source_entity_id,
            target_id=item.relationship_proposal.target_entity_id,
            type=item.relationship_proposal.type,
            evidence=assertion,
            confidence=confidence.score,
            attributes={"reviewed_mapping": True},
        )
        approved_relationship = KnowledgeRelationshipAssertion(
            relationship=relationship,
            assertion=assertion,
            knowledge_origin=knowledge_origin,
            supporting_evidence_ids=[card.id for card in evidence_cards],
            contradicting_evidence_ids=_contradicting_ids(evidence_cards),
            limitations=limitations,
            confidence=confidence,
            review_item_id=item.id,
            reviewer=human,
            reviewed_at=now,
        )
        return self._approve(
            item,
            human,
            reason,
            now,
            approved_relationship=approved_relationship,
        )

    def reject(self, item_id: str, *, reviewer: str, reason: str) -> MappingReviewItem:
        item = self._pending(item_id)
        try:
            human = require_human_identity(reviewer)
        except ValueError as exc:
            raise MappingReviewError(str(exc)) from exc
        if not reason.strip():
            raise MappingReviewError("mapping rejection reason is required")
        rejected = MappingReviewItem.model_validate(
            {
                **item.model_dump(mode="python"),
                "status": ReviewStatus.REJECTED,
                "reviewer": human,
                "reviewed_at": datetime.now(UTC),
                "reason": reason.strip(),
                "audit_log": [
                    *item.audit_log,
                    ReviewEvent(
                        action="mapping_rejected",
                        actor=human,
                        notes=reason.strip(),
                    ),
                ],
            }
        )
        self.repository.save(rejected)
        return rejected

    def _submit(self, item: MappingReviewItem) -> MappingReviewItem:
        existing = self.repository.get(item.id)
        if existing:
            return existing
        self.repository.save(item)
        return item

    def _pending(self, item_id: str) -> MappingReviewItem:
        item = self.repository.get(item_id)
        if item is None:
            raise MappingReviewError(f"mapping review item not found: {item_id}")
        if item.status != ReviewStatus.PENDING:
            raise MappingReviewError(
                f"invalid mapping review transition from {item.status.value}"
            )
        return item

    def _validate_decision(
        self,
        expected_card_ids: list[str],
        cards: list[EvidenceCard],
        reviewer: str,
        reason: str,
        confidence: ConfidenceAssessment,
        knowledge_origin: KnowledgeOrigin,
    ) -> tuple[str, list[EvidenceCard]]:
        try:
            human = require_human_identity(reviewer)
        except ValueError as exc:
            raise MappingReviewError(str(exc)) from exc
        if not reason.strip():
            raise MappingReviewError("mapping approval reason is required")
        if confidence.assessed_by != human:
            raise MappingReviewError("mapping confidence must be assessed by reviewer")
        by_id = {card.id: card for card in cards}
        if set(by_id) != set(expected_card_ids):
            raise MappingReviewError("mapping review must use its exact supporting cards")
        evidence_cards = [by_id[card_id] for card_id in expected_card_ids]
        if any(not card.review.approved for card in evidence_cards):
            raise MappingReviewError("mapping requires approved Evidence Cards")
        if any(card.knowledge_origin != knowledge_origin for card in evidence_cards):
            raise MappingReviewError("mapping cannot blend different knowledge origins")
        return human, evidence_cards

    def _approve(
        self,
        item: MappingReviewItem,
        reviewer: str,
        reason: str,
        reviewed_at: datetime,
        *,
        approved_node: KnowledgeNodeVersion | None = None,
        approved_relationship: KnowledgeRelationshipAssertion | None = None,
    ) -> MappingReviewItem:
        approved = MappingReviewItem.model_validate(
            {
                **item.model_dump(mode="python"),
                "status": ReviewStatus.APPROVED,
                "reviewer": reviewer,
                "reviewed_at": reviewed_at,
                "reason": reason.strip(),
                "approved_node": approved_node,
                "approved_relationship": approved_relationship,
                "audit_log": [
                    *item.audit_log,
                    ReviewEvent(
                        action="mapping_approved",
                        actor=reviewer,
                        notes=reason.strip(),
                    ),
                ],
            }
        )
        self.repository.save(approved)
        return approved


def _contradicting_ids(cards: list[EvidenceCard]) -> list[str]:
    return list(
        dict.fromkeys(
            card_id
            for card in cards
            for card_id in card.contradicting_evidence_ids
        )
    )
