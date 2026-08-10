"""Lazy public exports for review services without package import cycles."""

from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "ApprovedKnowledgeIngestor": ("research.review.service", "ApprovedKnowledgeIngestor"),
    "HumanReviewService": ("research.review.service", "HumanReviewService"),
    "ReviewError": ("research.review.service", "ReviewError"),
    "ReviewEvent": ("research.review.models", "ReviewEvent"),
    "ReviewItem": ("research.review.models", "ReviewItem"),
    "ReviewStatus": ("research.review.models", "ReviewStatus"),
    "ReviewRepository": ("research.review.repository", "ReviewRepository"),
    "InMemoryReviewRepository": ("research.review.repository", "InMemoryReviewRepository"),
    "JsonReviewRepository": ("research.review.repository", "JsonReviewRepository"),
    "ClaimReviewError": ("research.review.claim_service", "ClaimReviewError"),
    "ClaimReviewService": ("research.review.claim_service", "ClaimReviewService"),
    "ClaimReviewItem": ("research.review.claim_models", "ClaimReviewItem"),
    "ClaimReviewRepository": ("research.review.claim_repository", "ClaimReviewRepository"),
    "InMemoryClaimReviewRepository": ("research.review.claim_repository", "InMemoryClaimReviewRepository"),
    "JsonClaimReviewRepository": ("research.review.claim_repository", "JsonClaimReviewRepository"),
    "ExtractionScope": ("research.review.source_models", "ExtractionScope"),
    "SourceApprovalRecord": ("research.review.source_models", "SourceApprovalRecord"),
    "SourceApprovalError": ("research.review.source_service", "SourceApprovalError"),
    "SourceApprovalService": ("research.review.source_service", "SourceApprovalService"),
    "candidate_content_hash": ("research.review.source_service", "candidate_content_hash"),
    "SourceApprovalRepository": ("research.review.source_repository", "SourceApprovalRepository"),
    "InMemorySourceApprovalRepository": ("research.review.source_repository", "InMemorySourceApprovalRepository"),
    "JsonSourceApprovalRepository": ("research.review.source_repository", "JsonSourceApprovalRepository"),
    "MappingReviewError": ("research.review.mapping", "MappingReviewError"),
    "MappingReviewItem": ("research.review.mapping", "MappingReviewItem"),
    "MappingReviewRepository": ("research.review.mapping", "MappingReviewRepository"),
    "MappingReviewService": ("research.review.mapping", "MappingReviewService"),
    "InMemoryMappingReviewRepository": ("research.review.mapping", "InMemoryMappingReviewRepository"),
    "JsonMappingReviewRepository": ("research.review.mapping", "JsonMappingReviewRepository"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
