"""Named-human review of individual Evidence Cards."""

from __future__ import annotations

from datetime import UTC, datetime

from knowledge.projections import IngestionReceipt
from knowledge.evidence import (
    ConfidenceAssessment,
    ContradictionStatus,
    EvidenceCard,
    EvidenceReview,
)
from knowledge.governance import (
    ClaimEligibility,
    ContradictionCheckStatus,
    ReviewStatus,
    ReviewEvent,
    SecretExposureLevel,
    SensitiveInformationLevel,
    StoragePermission,
    require_human_identity,
)
from research.review.claim_models import ClaimReviewItem
from research.review.claim_repository import ClaimReviewRepository


class ClaimReviewError(ValueError):
    pass


class ClaimReviewService:
    def __init__(self, repository: ClaimReviewRepository) -> None:
        self.repository = repository

    def submit(
        self,
        card: EvidenceCard,
        *,
        actor: str = "research-pipeline",
    ) -> ClaimReviewItem:
        if card.review.review_status != ReviewStatus.PENDING or card.review.approved:
            raise ClaimReviewError("only pending Evidence Card candidates may be submitted")
        draft = ClaimReviewItem(
            card=card,
            audit_log=[ReviewEvent(action="claim_submitted", actor=actor)],
        )
        existing = self.repository.get(draft.id)
        if existing:
            return existing
        review = card.review.model_copy(update={"review_item_id": draft.id})
        item = ClaimReviewItem.model_validate(
            {
                **draft.model_dump(mode="python"),
                "card": card.model_copy(update={"review": review}),
            }
        )
        self.repository.save(item)
        return item

    def approve(
        self,
        item_id: str,
        *,
        reviewer: str,
        reason: str,
        confidence: ConfidenceAssessment,
        claim_eligibility: ClaimEligibility,
        storage_permission: StoragePermission,
        sensitive_information_level: SensitiveInformationLevel,
        contradiction_status: ContradictionStatus,
        contradicting_evidence_checked: ContradictionCheckStatus,
        limitations: list[str],
        contradicting_evidence_ids: list[str] | None = None,
        secret_exposure_level: SecretExposureLevel | None = None,
    ) -> ClaimReviewItem:
        item = self._pending(item_id)
        try:
            human = require_human_identity(reviewer)
        except ValueError as exc:
            raise ClaimReviewError(str(exc)) from exc
        if not reason.strip():
            raise ClaimReviewError("claim approval reason is required")
        if confidence.assessed_by.strip() != human:
            raise ClaimReviewError("confidence assessment must be made by the reviewer")
        if not claim_eligibility.allows_projection:
            raise ClaimReviewError("approved claim must be eligible")
        if not contradicting_evidence_checked.completed:
            raise ClaimReviewError("claim approval requires a contradiction check")
        if contradiction_status == ContradictionStatus.NOT_CHECKED:
            raise ClaimReviewError("claim approval requires contradiction status")
        conflict_ids = contradicting_evidence_ids or []
        if (
            contradiction_status
            in {ContradictionStatus.RESOLVED, ContradictionStatus.UNRESOLVED}
            and not conflict_ids
        ):
            raise ClaimReviewError("resolved/unresolved contradictions require linked cards")
        if not [value for value in limitations if value.strip()]:
            raise ClaimReviewError("claim approval requires explicit limitations")
        if _storage_rank(storage_permission) > _storage_rank(
            item.card.review.storage_permission
        ):
            raise ClaimReviewError("claim storage permission exceeds source approval")

        now = datetime.now(UTC)
        review = EvidenceReview(
            review_item_id=item.id,
            claim_eligibility=claim_eligibility,
            extraction_permission=item.card.review.extraction_permission,
            storage_permission=storage_permission,
            approved=True,
            review_status=ReviewStatus.APPROVED,
            reviewer=human,
            review_date=now,
            approval_reason=reason.strip(),
            contradicting_evidence_checked=contradicting_evidence_checked,
            sensitive_information_level=sensitive_information_level,
        )
        card = EvidenceCard.model_validate(
            {
                **item.card.model_dump(mode="python"),
                "limitations": [value.strip() for value in limitations if value.strip()],
                "confidence": confidence,
                "contradiction_status": contradiction_status,
                "contradicting_evidence_ids": conflict_ids,
                "secret_exposure_level": (
                    secret_exposure_level or item.card.secret_exposure_level
                ),
                "review": review,
            }
        )
        approved = ClaimReviewItem.model_validate(
            {
                **item.model_dump(mode="python"),
                "card": card,
                "status": ReviewStatus.APPROVED,
                "reviewed_at": now,
                "audit_log": [
                    *item.audit_log,
                    ReviewEvent(
                        action="claim_approved",
                        actor=human,
                        notes=reason.strip(),
                    ),
                ],
            }
        )
        self.repository.save(approved)
        return approved

    def reject(self, item_id: str, *, reviewer: str, reason: str) -> ClaimReviewItem:
        item = self._pending(item_id)
        try:
            human = require_human_identity(reviewer)
        except ValueError as exc:
            raise ClaimReviewError(str(exc)) from exc
        if not reason.strip():
            raise ClaimReviewError("claim rejection reason is required")
        now = datetime.now(UTC)
        review = item.card.review.model_copy(
            update={
                "approved": False,
                "review_status": ReviewStatus.REJECTED,
                "reviewer": human,
                "review_date": now,
                "approval_reason": reason.strip(),
            }
        )
        rejected = ClaimReviewItem.model_validate(
            {
                **item.model_dump(mode="python"),
                "card": item.card.model_copy(update={"review": review}),
                "status": ReviewStatus.REJECTED,
                "reviewed_at": now,
                "audit_log": [
                    *item.audit_log,
                    ReviewEvent(
                        action="claim_rejected",
                        actor=human,
                        notes=reason.strip(),
                    ),
                ],
            }
        )
        self.repository.save(rejected)
        return rejected

    def get(self, item_id: str) -> ClaimReviewItem:
        item = self.repository.get(item_id)
        if not item:
            raise ClaimReviewError(f"claim review item not found: {item_id}")
        return item

    def list(self, status: ReviewStatus | None = None) -> list[ClaimReviewItem]:
        return self.repository.list(status)

    def mark_ingested(
        self,
        item_ids: list[str],
        receipt: IngestionReceipt,
        *,
        actor: str,
    ) -> list[ClaimReviewItem]:
        unique_ids = list(dict.fromkeys(item_ids))
        items = [self.get(item_id) for item_id in unique_ids]
        invalid = [
            item.id
            for item in items
            if item.status not in {ReviewStatus.APPROVED, ReviewStatus.INGESTED}
        ]
        if invalid:
            raise ClaimReviewError(
                "only approved claims may be marked ingested: " + ", ".join(invalid)
            )
        updated: list[ClaimReviewItem] = []
        for item in items:
            if item.status == ReviewStatus.INGESTED:
                updated.append(item)
                continue
            review = item.card.review.model_copy(
                update={"review_status": ReviewStatus.INGESTED}
            )
            ingested = ClaimReviewItem.model_validate(
                {
                    **item.model_dump(mode="python"),
                    "card": item.card.model_copy(update={"review": review}),
                    "status": ReviewStatus.INGESTED,
                    "ingested_at": receipt.ingested_at,
                    "audit_log": [
                        *item.audit_log,
                        ReviewEvent(
                            action="claim_ingested",
                            actor=actor,
                            notes=f"receipt {receipt.id}",
                        ),
                    ],
                }
            )
            updated.append(ingested)
        for item in updated:
            if item.status == ReviewStatus.INGESTED:
                self.repository.save(item)
        return updated

    def _pending(self, item_id: str) -> ClaimReviewItem:
        item = self.get(item_id)
        if item.status != ReviewStatus.PENDING:
            raise ClaimReviewError(
                f"invalid claim review transition from {item.status.value}"
            )
        return item


def _storage_rank(permission: StoragePermission) -> int:
    return {
        StoragePermission.NONE: 0,
        StoragePermission.DERIVED_KNOWLEDGE_ONLY: 1,
        StoragePermission.DERIVED_WITH_SHORT_EXCERPT: 2,
    }[permission]
