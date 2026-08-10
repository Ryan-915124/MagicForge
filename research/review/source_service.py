"""Human-only source approval service for exact, content-addressed source versions."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from research.citation.manager import CitationManager
from research.citation.models import CitationRecord, CitationStatus
from knowledge.evidence import SourceType
from knowledge.governance import (
    ClaimEligibility,
    ContradictionCheckStatus,
    ExtractionPermission,
    ReviewStatus,
    ReviewEvent,
    SensitiveInformationLevel,
    StoragePermission,
    require_human_identity,
)
from research.models import ContentAccess, ResearchCandidate, SourceCategory
from research.review.source_models import ExtractionScope, SourceApprovalRecord
from research.review.source_repository import SourceApprovalRepository


class SourceApprovalError(ValueError):
    pass


class SourceApprovalService:
    def __init__(
        self,
        repository: SourceApprovalRepository,
        citations: CitationManager | None = None,
    ) -> None:
        self.repository = repository
        self.citations = citations or CitationManager()

    def submit(
        self,
        candidate: ResearchCandidate,
        citation: CitationRecord,
        *,
        content_hash: str | None = None,
        content_access: ContentAccess | None = None,
        source_type: SourceType | None = None,
        extraction_scope: ExtractionScope | None = None,
        actor: str = "research-pipeline",
    ) -> SourceApprovalRecord:
        if citation.source_candidate_id != candidate.id:
            raise SourceApprovalError("citation must refer to the source candidate")
        validated_citation = self.citations.validate(citation)
        checksum = content_hash or candidate_content_hash(candidate)
        item = SourceApprovalRecord(
            source_candidate_id=candidate.id,
            citation_id=validated_citation.id,
            citation_status=validated_citation.status,
            content_hash=checksum,
            content_access=content_access or candidate.content_access,
            source_type=source_type or _default_source_type(candidate.source_category),
            extraction_scope=extraction_scope or ExtractionScope(),
            audit_log=[ReviewEvent(action="source_submitted", actor=actor)],
        )
        existing = self.repository.get(item.id)
        if existing:
            return existing
        self.repository.save(item)
        return item

    def approve(
        self,
        item_id: str,
        *,
        reviewer: str,
        reason: str,
        claim_eligibility: ClaimEligibility,
        extraction_permission: ExtractionPermission,
        storage_permission: StoragePermission,
        sensitive_information_level: SensitiveInformationLevel,
        source_type: SourceType | None = None,
        contradicting_evidence_checked: ContradictionCheckStatus = (
            ContradictionCheckStatus.NOT_APPLICABLE
        ),
    ) -> SourceApprovalRecord:
        item = self._pending(item_id)
        try:
            human = require_human_identity(reviewer)
        except ValueError as exc:
            raise SourceApprovalError(str(exc)) from exc
        if not reason.strip():
            raise SourceApprovalError("source approval reason is required")
        if not claim_eligibility.allows_projection:
            raise SourceApprovalError("source must be claim-eligible")
        if not extraction_permission.allows_claim_extraction:
            raise SourceApprovalError("source must permit selected or full-text extraction")
        if item.citation_status not in {
            CitationStatus.METADATA_VERIFIED,
            CitationStatus.FULL_TEXT_VERIFIED,
        }:
            raise SourceApprovalError("citation must be verified before source approval")
        now = datetime.now(UTC)
        approved = SourceApprovalRecord.model_validate(
            {
                **item.model_dump(mode="python"),
                "status": ReviewStatus.APPROVED,
                "reviewer": human,
                "review_date": now,
                "approval_reason": reason.strip(),
                "claim_eligibility": claim_eligibility,
                "extraction_permission": extraction_permission,
                "storage_permission": storage_permission,
                "sensitive_information_level": sensitive_information_level,
                "source_type": source_type or item.source_type,
                "contradicting_evidence_checked": contradicting_evidence_checked,
                "audit_log": [
                    *item.audit_log,
                    ReviewEvent(
                        action="source_approved",
                        actor=human,
                        notes=reason.strip(),
                    ),
                ],
            }
        )
        self.repository.save(approved)
        return approved

    def reject(self, item_id: str, *, reviewer: str, reason: str) -> SourceApprovalRecord:
        item = self._pending(item_id)
        try:
            human = require_human_identity(reviewer)
        except ValueError as exc:
            raise SourceApprovalError(str(exc)) from exc
        if not reason.strip():
            raise SourceApprovalError("source rejection reason is required")
        rejected = SourceApprovalRecord.model_validate(
            {
                **item.model_dump(mode="python"),
                "status": ReviewStatus.REJECTED,
                "reviewer": human,
                "review_date": datetime.now(UTC),
                "approval_reason": reason.strip(),
                "audit_log": [
                    *item.audit_log,
                    ReviewEvent(
                        action="source_rejected",
                        actor=human,
                        notes=reason.strip(),
                    ),
                ],
            }
        )
        self.repository.save(rejected)
        return rejected

    def get(self, item_id: str) -> SourceApprovalRecord:
        item = self.repository.get(item_id)
        if not item:
            raise SourceApprovalError(f"source review item not found: {item_id}")
        return item

    def _pending(self, item_id: str) -> SourceApprovalRecord:
        item = self.get(item_id)
        if item.status != ReviewStatus.PENDING:
            raise SourceApprovalError(
                f"invalid source review transition from {item.status.value}"
            )
        return item


def candidate_content_hash(candidate: ResearchCandidate) -> str:
    if candidate.content_access == ContentAccess.SEARCH_SNIPPET or not candidate.content:
        raise SourceApprovalError(
            "source approval requires acquired content, not a search snippet"
        )
    return hashlib.sha256(candidate.content.encode("utf-8")).hexdigest()


def _default_source_type(category: SourceCategory) -> SourceType:
    if category == SourceCategory.ACADEMIC:
        return SourceType.JOURNAL_ARTICLE
    return SourceType.WEB_ARTICLE
