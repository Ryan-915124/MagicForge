"""Human-reviewed knowledge acquisition for MagicForge."""

from __future__ import annotations

from research.models import (
    ContentAccess,
    DiscoveryBatch,
    DiscoveryProvider,
    ResearchCandidate,
    ResearchProtocol,
    SourceCategory,
)

__all__ = [
    "ContentAccess",
    "DiscoveryBatch",
    "DiscoveryProvider",
    "KnowledgeAcquisitionWorkflow",
    "ResearchCandidate",
    "ResearchProtocol",
    "SourceCategory",
]


def __getattr__(name: str):
    if name != "KnowledgeAcquisitionWorkflow":
        raise AttributeError(name)
    from research.pipeline import KnowledgeAcquisitionWorkflow

    globals()[name] = KnowledgeAcquisitionWorkflow
    return KnowledgeAcquisitionWorkflow
