"""Citation normalization and verification gates."""

from research.citation.manager import CitationManager, CitationValidationError
from research.citation.models import CitationRecord, CitationStatus

__all__ = ["CitationManager", "CitationRecord", "CitationStatus", "CitationValidationError"]
