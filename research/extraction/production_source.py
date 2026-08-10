"""Fail-closed adapter from Production governance rows to GLM extraction input.

The public API may list readiness metadata, but source text is only materialized
inside :class:`ApprovedSourceEnvelope` after every current-approval invariant is
revalidated against PostgreSQL.
"""

from __future__ import annotations

import hashlib
from datetime import UTC
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from knowledge.governance import (
    ExtractionPermission,
    ReviewStatus,
    SensitiveInformationLevel,
)
from persistence.models import (
    CitationVerificationEvidence,
    GovernanceWorkflowStatus,
    Source,
    SourceReviewDecision,
    SourceVersion,
    User,
)
from research.citation.models import CitationRecord, CitationStatus, PeerReviewStatus
from research.models import ContentAccess, ResearchCandidate, SourceCategory
from research.review.source_models import ExtractionScope, SourceApprovalRecord
from research.review.source_permission_policy import (
    SourcePermissionPolicyError,
    require_current_source_approval,
)


class ProductionSourceReadinessError(ValueError):
    """Stable, non-sensitive rejection raised before source text leaves storage."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.public_message = message
        super().__init__(message)


class EligibleSourceView(BaseModel):
    """Content-free view safe for an operator extraction queue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: UUID
    source_version_id: UUID
    source_review_decision_id: UUID
    source_permission_request_id: UUID
    title: str
    source_category: SourceCategory
    source_type: str
    content_access: ContentAccess
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    extraction_permission: ExtractionPermission
    extraction_scope_locators: list[str] = Field(default_factory=list)
    sensitivity: SensitiveInformationLevel
    citation_status: CitationStatus


class ApprovedSourceEnvelope(BaseModel):
    """Internal exact-version input for one governed extraction run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: UUID
    source_version_id: UUID
    source_review_decision_id: UUID
    source_permission_request_id: UUID
    sensitivity: SensitiveInformationLevel
    candidate: ResearchCandidate
    citation: CitationRecord
    approval: SourceApprovalRecord

    def readiness_view(self) -> EligibleSourceView:
        return EligibleSourceView(
            source_id=self.source_id,
            source_version_id=self.source_version_id,
            source_review_decision_id=self.source_review_decision_id,
            source_permission_request_id=self.source_permission_request_id,
            title=self.candidate.title,
            source_category=self.candidate.source_category,
            source_type=self.approval.source_type.value,
            content_access=self.candidate.content_access,
            content_hash=self.approval.content_hash,
            extraction_permission=self.approval.extraction_permission,
            extraction_scope_locators=list(self.approval.extraction_scope.locators),
            sensitivity=self.sensitivity,
            citation_status=self.citation.status,
        )


class ProductionSourceAdapter:
    """Load exact Source versions only while their independent approval is current."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def list_eligible(self, *, limit: int = 50, offset: int = 0) -> list[EligibleSourceView]:
        if not 1 <= limit <= 100 or offset < 0:
            raise ProductionSourceReadinessError(
                "invalid_pagination", "Pagination parameters are invalid."
            )
        with self._session_factory() as session:
            latest_versions = (
                select(
                    SourceVersion.source_id.label("source_id"),
                    func.max(SourceVersion.version_number).label("version_number"),
                )
                .group_by(SourceVersion.source_id)
                .subquery()
            )
            rows = list(
                session.scalars(
                    select(SourceVersion)
                    .join(
                        latest_versions,
                        (latest_versions.c.source_id == SourceVersion.source_id)
                        & (
                            latest_versions.c.version_number
                            == SourceVersion.version_number
                        ),
                    )
                    .order_by(SourceVersion.created_at.asc(), SourceVersion.id.asc())
                )
            )
            eligible: list[EligibleSourceView] = []
            for version in rows:
                try:
                    envelope = self._load_from_session(session, version)
                    ProductionGLMOutboundPolicy.require_allowed(
                        envelope, extraction_enabled=True
                    )
                except ProductionSourceReadinessError:
                    continue
                eligible.append(envelope.readiness_view())
            return eligible[offset : offset + limit]

    def load(self, source_version_id: UUID) -> ApprovedSourceEnvelope:
        with self._session_factory() as session:
            version = session.get(SourceVersion, source_version_id)
            if version is None:
                raise ProductionSourceReadinessError(
                    "source_version_not_found", "The Source version is unavailable."
                )
            return self._load_from_session(session, version)

    def _load_from_session(
        self, session: Session, version: SourceVersion
    ) -> ApprovedSourceEnvelope:
        newest_version_number = session.scalar(
            select(func.max(SourceVersion.version_number)).where(
                SourceVersion.source_id == version.source_id
            )
        )
        if newest_version_number != version.version_number:
            raise ProductionSourceReadinessError(
                "source_version_superseded",
                "Only the newest immutable Source version may be extracted.",
            )
        decision = session.scalar(
            select(SourceReviewDecision)
            .where(SourceReviewDecision.source_version_id == version.id)
            .order_by(SourceReviewDecision.sequence_number.desc())
            .limit(1)
        )
        if (
            decision is None
            or decision.resulting_status != GovernanceWorkflowStatus.APPROVED
        ):
            raise ProductionSourceReadinessError(
                "source_not_approved",
                "The exact Source version is not approved for extraction.",
            )
        try:
            permission_request = require_current_source_approval(
                session, version, decision
            )
        except SourcePermissionPolicyError as exc:
            raise ProductionSourceReadinessError(exc.code, str(exc)) from exc

        actual_hash = hashlib.sha256(version.content_text.encode("utf-8")).hexdigest()
        if actual_hash != version.content_hash:
            raise ProductionSourceReadinessError(
                "source_content_hash_mismatch",
                "The immutable Source content failed its integrity check.",
            )
        if decision.citation_verification_evidence_id is None:
            raise ProductionSourceReadinessError(
                "citation_not_verified",
                "The Source approval is not bound to citation verification evidence.",
            )
        citation_evidence = session.get(
            CitationVerificationEvidence,
            decision.citation_verification_evidence_id,
        )
        if (
            citation_evidence is None
            or citation_evidence.source_version_id != version.id
            or citation_evidence.citation_id != version.citation_id
        ):
            raise ProductionSourceReadinessError(
                "citation_verification_binding_invalid",
                "Citation verification is not bound to this exact Source version.",
            )
        reviewer = session.get(User, decision.reviewer_user_id)
        if reviewer is None:
            raise ProductionSourceReadinessError(
                "source_reviewer_unavailable",
                "The independent Source reviewer is unavailable.",
            )

        citation_data = dict(version.citation_metadata)
        try:
            category = SourceCategory(
                citation_data.pop("source_category", SourceCategory.WEB.value)
            )
            peer_review = PeerReviewStatus(
                citation_data.get("peer_review_status", PeerReviewStatus.UNKNOWN.value)
            )
            provenance = citation_data.get("provenance") or []
            candidate = ResearchCandidate(
                id=str(version.source_id),
                title=str(citation_data.get("title") or ""),
                url=str(citation_data.get("url") or ""),
                authors=[str(value) for value in citation_data.get("authors") or []],
                published_year=citation_data.get("year"),
                venue=str(citation_data.get("venue") or ""),
                doi=citation_data.get("doi"),
                snippet="",
                content=version.content_text,
                content_access=version.content_access,
                source_category=category,
                provenance=provenance,
            )
            citation = CitationRecord(
                source_candidate_id=candidate.id,
                source_category=category,
                title=candidate.title,
                authors=candidate.authors,
                year=candidate.published_year,
                venue=candidate.venue,
                volume=str(citation_data.get("volume") or ""),
                issue=str(citation_data.get("issue") or ""),
                pages=str(citation_data.get("pages") or ""),
                doi=candidate.doi,
                url=candidate.url,
                peer_review_status=peer_review,
                status=(
                    CitationStatus.FULL_TEXT_VERIFIED
                    if citation_evidence.verification_scope.value == "full_text"
                    else CitationStatus.METADATA_VERIFIED
                ),
                provenance=provenance,
            )
        except (TypeError, ValueError) as exc:
            raise ProductionSourceReadinessError(
                "source_metadata_invalid",
                "The approved Source metadata cannot form an extraction envelope.",
            ) from exc
        if UUID(citation.id) != version.citation_id:
            raise ProductionSourceReadinessError(
                "citation_identity_mismatch",
                "The verified citation identity no longer matches the Source version.",
            )

        approval = SourceApprovalRecord(
            source_candidate_id=candidate.id,
            citation_id=citation.id,
            citation_status=citation.status,
            source_permission_request_id=str(permission_request.id),
            content_hash=version.content_hash,
            content_access=version.content_access,
            source_type=version.source_type,
            extraction_scope=ExtractionScope(
                locators=[str(value) for value in decision.extraction_scope.get("locators", [])]
            ),
            status=ReviewStatus.APPROVED,
            reviewer=reviewer.username,
            review_date=decision.created_at.replace(tzinfo=UTC)
            if decision.created_at.tzinfo is None
            else decision.created_at.astimezone(UTC),
            approval_reason=decision.reason,
            claim_eligibility=decision.claim_eligibility,
            extraction_permission=decision.extraction_permission,
            storage_permission=decision.storage_permission,
            sensitive_information_level=decision.sensitive_information_level,
            contradicting_evidence_checked=decision.contradicting_evidence_checked,
        )
        if UUID(approval.source_version_id) != version.id:
            raise ProductionSourceReadinessError(
                "source_version_identity_mismatch",
                "The approved Source identity no longer matches its immutable version.",
            )
        return ApprovedSourceEnvelope(
            source_id=version.source_id,
            source_version_id=version.id,
            source_review_decision_id=decision.id,
            source_permission_request_id=permission_request.id,
            sensitivity=decision.sensitive_information_level,
            candidate=candidate,
            citation=citation,
            approval=approval,
        )


class ProductionGLMOutboundPolicy:
    """The final gate before approved source text is sent to Z.AI GLM."""

    _ALLOWED_SENSITIVITY = frozenset(
        {
            SensitiveInformationLevel.PUBLIC,
            SensitiveInformationLevel.CONTROLLED,
        }
    )

    @classmethod
    def require_allowed(
        cls,
        envelope: ApprovedSourceEnvelope,
        *,
        extraction_enabled: bool,
    ) -> None:
        if not extraction_enabled:
            raise ProductionSourceReadinessError(
                "production_glm_extraction_disabled",
                "Production GLM extraction is disabled by configuration.",
            )
        if envelope.sensitivity not in cls._ALLOWED_SENSITIVITY:
            raise ProductionSourceReadinessError(
                "source_sensitivity_blocks_external_llm",
                "This Source sensitivity level cannot be sent to an external LLM.",
            )
        if envelope.candidate.content_access in {
            ContentAccess.SEARCH_SNIPPET,
            ContentAccess.ABSTRACT,
        }:
            raise ProductionSourceReadinessError(
                "source_content_not_extractable",
                "Claim extraction requires exact claim-level Source content.",
            )
        if not envelope.approval.allows_claim_extraction:
            raise ProductionSourceReadinessError(
                "source_approval_not_extractable",
                "The current Source approval does not permit claim extraction.",
            )


__all__ = [
    "ApprovedSourceEnvelope",
    "EligibleSourceView",
    "ProductionGLMOutboundPolicy",
    "ProductionSourceAdapter",
    "ProductionSourceReadinessError",
]
