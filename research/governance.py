"""Compatibility exports for governance models now owned by the knowledge domain."""

from knowledge.governance import (
    ClaimEligibility,
    ClearanceRank,
    ContradictionCheckStatus,
    ExtractionPermission,
    ReviewEvent,
    ReviewStatus,
    SecretExposureLevel,
    SensitiveInformationLevel,
    StoragePermission,
    require_human_identity,
)

__all__ = [
    "ClaimEligibility",
    "ClearanceRank",
    "ContradictionCheckStatus",
    "ExtractionPermission",
    "ReviewEvent",
    "ReviewStatus",
    "SecretExposureLevel",
    "SensitiveInformationLevel",
    "StoragePermission",
    "require_human_identity",
]
