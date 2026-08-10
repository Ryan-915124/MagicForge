"""Lazy public exports for research extraction."""

from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "ResearchExtractionError": ("research.extraction.extractor", "ResearchExtractionError"),
    "ResearchKnowledgeExtractor": ("research.extraction.extractor", "ResearchKnowledgeExtractor"),
    "ExtractedClaim": ("research.extraction.models", "ExtractedClaim"),
    "ExtractedEntity": ("research.extraction.models", "ExtractedEntity"),
    "ExtractedRelationship": ("research.extraction.models", "ExtractedRelationship"),
    "EntityMappingProposal": ("research.extraction.models", "EntityMappingProposal"),
    "RelationshipMappingProposal": ("research.extraction.models", "RelationshipMappingProposal"),
    "KnowledgeProposal": ("research.extraction.models", "KnowledgeProposal"),
    "ResearchExtractionResult": ("research.extraction.models", "ResearchExtractionResult"),
    "ProductionBridgeError": (
        "research.extraction.production_bridge",
        "ProductionBridgeError",
    ),
    "ProductionClaimSubmission": (
        "research.extraction.production_bridge",
        "ProductionClaimSubmission",
    ),
    "UnresolvedEntityReference": (
        "research.extraction.production_bridge",
        "UnresolvedEntityReference",
    ),
    "build_production_claim_submissions": (
        "research.extraction.production_bridge",
        "build_production_claim_submissions",
    ),
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
