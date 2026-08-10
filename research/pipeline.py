"""Source-approved acquisition workflow that stops at atomic claim review."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from research.citation.manager import CitationManager
from research.citation.models import CitationRecord
from research.extraction.extractor import ResearchKnowledgeExtractor
from research.extraction.models import KnowledgeProposal
from research.models import (
    ContentAccess,
    DiscoveryBatch,
    ResearchCandidate,
    ResearchProtocol,
)
from research.review.claim_models import ClaimReviewItem
from research.review.claim_service import ClaimReviewService
from research.review.mapping import MappingReviewItem, MappingReviewService
from research.review.source_models import ExtractionScope, SourceApprovalRecord
from research.review.source_service import SourceApprovalService
from research.search.coordinator import ResearchSearchCoordinator


class ExtractionReviewSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_reviews: list[ClaimReviewItem] = Field(default_factory=list)
    mapping_reviews: list[MappingReviewItem] = Field(default_factory=list)


class KnowledgeAcquisitionWorkflow:
    """Orchestrate G1 and extraction; it intentionally has no ingestion method."""

    def __init__(
        self,
        search: ResearchSearchCoordinator,
        extractor: ResearchKnowledgeExtractor,
        source_review: SourceApprovalService,
        claim_review: ClaimReviewService,
        mapping_review: MappingReviewService,
        citations: CitationManager | None = None,
    ) -> None:
        self.search = search
        self.extractor = extractor
        self.source_review = source_review
        self.claim_review = claim_review
        self.mapping_review = mapping_review
        self.citations = citations or CitationManager()

    def discover(self, protocol: ResearchProtocol) -> DiscoveryBatch:
        return self.search.discover(protocol)

    def submit_source_for_review(
        self,
        candidate: ResearchCandidate,
        *,
        citation: CitationRecord | None = None,
        local_file: str | Path | None = None,
        extraction_scope: ExtractionScope | None = None,
        actor: str = "research-pipeline",
    ) -> SourceApprovalRecord:
        record = self.citations.validate(citation or self.citations.from_candidate(candidate))
        if local_file is None:
            return self.source_review.submit(
                candidate,
                record,
                extraction_scope=extraction_scope,
                actor=actor,
            )
        path = Path(local_file)
        try:
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError(f"could not read source file for review: {exc}") from exc
        access = (
            ContentAccess.PDF_TEXT
            if path.suffix.casefold() == ".pdf"
            else ContentAccess.FULL_TEXT
        )
        return self.source_review.submit(
            candidate,
            record,
            content_hash=checksum,
            content_access=access,
            extraction_scope=extraction_scope,
            actor=actor,
        )

    def create_proposal(
        self,
        candidate: ResearchCandidate,
        *,
        source_approval_id: str,
        citation: CitationRecord | None = None,
        local_file: str | Path | None = None,
    ) -> KnowledgeProposal:
        record = self.citations.validate(citation or self.citations.from_candidate(candidate))
        source_approval = self.source_review.get(source_approval_id)
        if local_file is not None:
            return self.extractor.extract_file(
                local_file,
                candidate,
                record,
                source_approval,
            )
        return self.extractor.extract_candidate(
            candidate,
            record,
            source_approval,
        )

    def submit_for_review(
        self,
        proposal: KnowledgeProposal,
        *,
        actor: str = "research-pipeline",
    ) -> ExtractionReviewSubmission:
        claim_reviews = [
            self.claim_review.submit(card, actor=actor)
            for card in proposal.evidence_cards
        ]
        mapping_reviews = [
            *[
                self.mapping_review.submit_entity(item, actor=actor)
                for item in proposal.entity_proposals
            ],
            *[
                self.mapping_review.submit_relationship(item, actor=actor)
                for item in proposal.relationship_proposals
            ],
        ]
        return ExtractionReviewSubmission(
            claim_reviews=claim_reviews,
            mapping_reviews=mapping_reviews,
        )
