"""Compatibility exports for the atomic claim-review repositories."""

from research.review.claim_repository import (
    ClaimReviewRepository as ReviewRepository,
    InMemoryClaimReviewRepository as InMemoryReviewRepository,
    JsonClaimReviewRepository as JsonReviewRepository,
)

__all__ = ["InMemoryReviewRepository", "JsonReviewRepository", "ReviewRepository"]
