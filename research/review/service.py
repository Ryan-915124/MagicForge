"""Atomic human claim review and the sole authorized Qdrant transition."""

from __future__ import annotations

from knowledge.manifest_repository import (
    IngestionReceiptRepository,
    StorageManifestRepository,
)
from knowledge.projections import IngestionReceipt, StorageManifestService
from knowledge.governance import require_human_identity
from research.review.claim_repository import ClaimReviewRepository
from research.review.claim_service import (
    ClaimReviewError,
    ClaimReviewService,
)
from retrieval.interfaces import ProjectionWriter


ReviewError = ClaimReviewError


class HumanReviewService(ClaimReviewService):
    """Public review service; its unit of approval is exactly one Evidence Card."""

    def __init__(self, repository: ClaimReviewRepository) -> None:
        super().__init__(repository)


class ApprovedKnowledgeIngestor:
    """Write only an exact, human-authorized Storage Manifest."""

    def __init__(
        self,
        manifests: StorageManifestRepository,
        receipts: IngestionReceiptRepository,
        writer: ProjectionWriter,
        claim_reviews: ClaimReviewService,
    ) -> None:
        self.manifests = manifests
        self.receipts = receipts
        self.writer = writer
        self.claim_reviews = claim_reviews
        self.manifest_service = StorageManifestService()

    def ingest(self, manifest_id: str, *, actor: str) -> IngestionReceipt:
        try:
            operator = require_human_identity(actor)
        except ValueError as exc:
            raise ReviewError(str(exc)) from exc
        manifest = self.manifests.get(manifest_id)
        if manifest is None:
            raise ReviewError(f"storage manifest not found: {manifest_id}")
        if not manifest.authorized:
            raise ReviewError("only a human-authorized manifest may be ingested")

        existing = self.receipts.get_for_manifest(manifest.id)
        if existing is not None:
            self._finalize(manifest, existing, operator)
            return existing
        try:
            receipt = self.writer.write_manifest(manifest, actor=operator)
            if receipt.point_ids != manifest.expected_point_ids:
                raise ReviewError("ingestion receipt does not match manifest point IDs")
            self.receipts.save(receipt)
            self._finalize(manifest, receipt, operator)
            return receipt
        except ReviewError:
            raise
        except Exception as exc:
            raise ReviewError(f"authorized manifest ingestion failed: {exc}") from exc

    def _finalize(
        self,
        manifest,
        receipt: IngestionReceipt,
        actor: str,
    ) -> None:
        self.claim_reviews.mark_ingested(
            [
                review_item_id
                for projection in manifest.projections
                for review_item_id in projection.claim_review_item_ids
            ],
            receipt,
            actor=actor,
        )
        if manifest.authorized:
            ingested_manifest = self.manifest_service.mark_ingested(manifest, receipt)
            self.manifests.save(ingested_manifest)
