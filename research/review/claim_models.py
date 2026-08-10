"""Atomic Evidence Card review records for the second human gate."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from knowledge.evidence import EvidenceCard
from knowledge.governance import ReviewEvent, ReviewStatus


class ClaimReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = ""
    card: EvidenceCard
    status: ReviewStatus = ReviewStatus.PENDING
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reviewed_at: datetime | None = None
    ingested_at: datetime | None = None
    audit_log: list[ReviewEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def assign_id_and_validate(self) -> "ClaimReviewItem":
        item_id = str(
            uuid5(
                NAMESPACE_URL,
                f"magicforge:claim-review:{self.card.id}:{self.card.version}",
            )
        )
        if self.id and self.id != item_id:
            raise ValueError("claim review ID does not match Evidence Card version")
        object.__setattr__(self, "id", item_id)
        if self.status != self.card.review.review_status:
            raise ValueError("claim review item and Evidence Card status must match")
        if self.status == ReviewStatus.INGESTED and self.ingested_at is None:
            raise ValueError("ingested claim review requires ingested_at")
        return self
