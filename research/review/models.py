"""Human-review state and immutable audit events."""

from __future__ import annotations

from knowledge.governance import ReviewEvent, ReviewStatus
from research.review.claim_models import ClaimReviewItem


# Public compatibility name now points at the atomic claim review object. It no
# longer represents or approves an entire KnowledgeProposal.
ReviewItem = ClaimReviewItem

__all__ = ["ReviewEvent", "ReviewItem", "ReviewStatus"]
