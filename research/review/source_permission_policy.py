"""Fail-closed policy for current Source permission requests and approvals."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge.governance import ExtractionPermission, StoragePermission
from persistence.idempotency import canonical_payload_hash
from persistence.models import (
    GovernanceWorkflowStatus,
    SourcePermissionRequest,
    SourceReviewDecision,
    SourceVersion,
)
from research.models import ContentAccess
from research.review.workflow_models import (
    SOURCE_PERMISSION_REQUEST_SCHEMA_VERSION,
)


LEGACY_SOURCE_PERMISSION_REQUEST_SCHEMA_VERSION = (
    "source-permission-request-legacy-0.1"
)
MAX_PERMISSION_REQUEST_CHAIN_LENGTH = 1_000


class SourcePermissionPolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def permission_request_checksum_payload(
    *,
    source_version_id: UUID,
    sequence_number: int,
    requested_extraction_permission: ExtractionPermission,
    requested_storage_permission: StoragePermission,
    requested_scope_locators: list[Any],
    rights_basis: str,
    rights_evidence: list[Any],
    reason: str,
    submitted_by_user_id: UUID,
    actor_role_snapshot: list[Any],
    supersedes_request_id: UUID | None,
) -> dict[str, Any]:
    return {
        "schema_version": SOURCE_PERMISSION_REQUEST_SCHEMA_VERSION,
        "source_version_id": str(source_version_id),
        "sequence": sequence_number,
        "requested_extraction_permission": requested_extraction_permission.value,
        "requested_storage_permission": requested_storage_permission.value,
        "requested_scope_locators": requested_scope_locators,
        "rights_basis": rights_basis,
        "rights_evidence": rights_evidence,
        "reason": reason,
        "submitted_by_user_id": str(submitted_by_user_id),
        "actor_role_snapshot": actor_role_snapshot,
        "supersedes_request_id": (
            str(supersedes_request_id)
            if supersedes_request_id is not None
            else None
        ),
    }


def require_permission_request_integrity(
    request: SourcePermissionRequest,
    version: SourceVersion,
) -> None:
    valid = False
    sequence_link_is_well_formed = bool(
        request.sequence_number >= 1
        and (
            (
                request.sequence_number == 1
                and request.supersedes_request_id is None
            )
            or (
                request.sequence_number > 1
                and request.supersedes_request_id is not None
            )
        )
    )
    if request.source_version_id == version.id and sequence_link_is_well_formed:
        if (
            request.domain_schema_version
            == LEGACY_SOURCE_PERMISSION_REQUEST_SCHEMA_VERSION
        ):
            valid = _legacy_integrity_valid(request, version)
        elif (
            request.domain_schema_version
            == SOURCE_PERMISSION_REQUEST_SCHEMA_VERSION
        ):
            payload = permission_request_checksum_payload(
                source_version_id=request.source_version_id,
                sequence_number=request.sequence_number,
                requested_extraction_permission=(
                    request.requested_extraction_permission
                ),
                requested_storage_permission=request.requested_storage_permission,
                requested_scope_locators=request.requested_scope_locators,
                rights_basis=request.rights_basis,
                rights_evidence=request.rights_evidence,
                reason=request.reason,
                submitted_by_user_id=request.submitted_by_user_id,
                actor_role_snapshot=request.actor_role_snapshot,
                supersedes_request_id=request.supersedes_request_id,
            )
            expected_checksum = canonical_payload_hash(payload)
            expected_id = uuid5(
                NAMESPACE_URL,
                (
                    "magicforge:source-permission-request:"
                    f"{request.source_version_id}:{request.sequence_number}:"
                    f"{expected_checksum}"
                ),
            )
            valid = bool(
                hmac.compare_digest(request.request_checksum, expected_checksum)
                and request.id == expected_id
            )
    if not valid:
        raise SourcePermissionPolicyError(
            "source_permission_request_integrity_failed",
            "The Source permission request failed its integrity check.",
        )


def require_current_source_approval(
    session: Session,
    version: SourceVersion,
    decision: SourceReviewDecision,
) -> SourcePermissionRequest:
    """Revalidate one exact current approval before any downstream use."""

    if (
        decision.source_version_id != version.id
        or decision.resulting_status != GovernanceWorkflowStatus.APPROVED
    ):
        raise SourcePermissionPolicyError(
            "source_approval_not_current",
            "The exact Source version does not have a current approval.",
        )
    latest_decision = session.scalar(
        select(SourceReviewDecision)
        .where(SourceReviewDecision.source_version_id == version.id)
        .order_by(SourceReviewDecision.sequence_number.desc())
        .limit(1)
    )
    if latest_decision is None or latest_decision.id != decision.id:
        raise SourcePermissionPolicyError(
            "source_approval_not_current",
            "The Source approval has been superseded by a later decision.",
        )
    request = session.get(
        SourcePermissionRequest,
        decision.source_permission_request_id,
    )
    if request is None or request.source_version_id != version.id:
        raise SourcePermissionPolicyError(
            "source_permission_request_binding_invalid",
            "The Source approval is not bound to an exact permission request.",
        )
    require_permission_request_integrity(request, version)
    latest = session.scalar(
        select(SourcePermissionRequest)
        .where(SourcePermissionRequest.source_version_id == version.id)
        .order_by(SourcePermissionRequest.sequence_number.desc())
        .limit(1)
    )
    if latest is None or latest.id != request.id:
        raise SourcePermissionPolicyError(
            "source_permission_request_stale",
            "The Source approval is not bound to the latest permission request.",
        )
    require_permission_request_chain_integrity(session, version, latest)
    request_submitters = set(
        session.scalars(
            select(SourcePermissionRequest.submitted_by_user_id).where(
                SourcePermissionRequest.source_version_id == version.id
            )
        )
    )
    if decision.reviewer_user_id in (
        request_submitters | {version.submitted_by_user_id}
    ):
        raise SourcePermissionPolicyError(
            "source_permission_request_independence_failed",
            "The Source approval lacks an independent reviewer.",
        )
    _require_valid_request_shape(request)
    _require_decision_within_request(version, request, decision)
    return request


def require_permission_request_chain_integrity(
    session: Session,
    version: SourceVersion,
    latest: SourcePermissionRequest,
) -> None:
    """Validate a bounded, contiguous append-only history through ``latest``."""

    rows = list(
        session.scalars(
            select(SourcePermissionRequest)
            .where(SourcePermissionRequest.source_version_id == version.id)
            .order_by(
                SourcePermissionRequest.sequence_number.asc(),
                SourcePermissionRequest.id.asc(),
            )
            .limit(MAX_PERMISSION_REQUEST_CHAIN_LENGTH + 1)
        )
    )
    if (
        not rows
        or len(rows) > MAX_PERMISSION_REQUEST_CHAIN_LENGTH
        or rows[-1].id != latest.id
    ):
        raise SourcePermissionPolicyError(
            "source_permission_request_chain_invalid",
            "The Source permission request history is missing or exceeds its safe bound.",
        )
    previous: SourcePermissionRequest | None = None
    seen_ids: set[UUID] = set()
    for expected_sequence, row in enumerate(rows, start=1):
        require_permission_request_integrity(row, version)
        if (
            row.id in seen_ids
            or row.sequence_number != expected_sequence
            or (
                previous is None
                and row.supersedes_request_id is not None
            )
            or (
                previous is not None
                and row.supersedes_request_id != previous.id
            )
        ):
            raise SourcePermissionPolicyError(
                "source_permission_request_chain_invalid",
                "The Source permission request history is not contiguous.",
            )
        seen_ids.add(row.id)
        previous = row


def _require_valid_request_shape(request: SourcePermissionRequest) -> None:
    scope = _string_list(request.requested_scope_locators)
    evidence = _string_list(request.rights_evidence)
    roles = _string_list(request.actor_role_snapshot)
    if (
        scope is None
        or evidence is None
        or not evidence
        or roles is None
        or not isinstance(request.rights_basis, str)
        or not request.rights_basis.strip()
        or not isinstance(request.reason, str)
        or not request.reason.strip()
    ):
        raise SourcePermissionPolicyError(
            "source_permission_request_policy_invalid",
            "The Source permission request has an invalid rights payload.",
        )
    selected = (
        request.requested_extraction_permission
        == ExtractionPermission.SELECTED_SECTIONS
    )
    if (selected and not scope) or (not selected and scope):
        raise SourcePermissionPolicyError(
            "source_permission_request_policy_invalid",
            "The Source permission request has an invalid extraction scope.",
        )
    if (
        request.requested_storage_permission != StoragePermission.NONE
        and request.requested_extraction_permission
        not in {
            ExtractionPermission.SELECTED_SECTIONS,
            ExtractionPermission.FULL_TEXT,
        }
    ):
        raise SourcePermissionPolicyError(
            "source_permission_request_policy_invalid",
            "The Source storage request lacks claim-level extraction authority.",
        )


def _require_decision_within_request(
    version: SourceVersion,
    request: SourcePermissionRequest,
    decision: SourceReviewDecision,
) -> None:
    if _extraction_rank(decision.extraction_permission) > _extraction_rank(
        request.requested_extraction_permission
    ):
        raise SourcePermissionPolicyError(
            "source_permission_request_ceiling_exceeded",
            "The Source decision exceeds the extraction request.",
        )
    if _storage_rank(decision.storage_permission) > _storage_rank(
        request.requested_storage_permission
    ):
        raise SourcePermissionPolicyError(
            "source_permission_request_ceiling_exceeded",
            "The Source decision exceeds the storage request.",
        )
    if not isinstance(decision.extraction_scope, dict):
        raise SourcePermissionPolicyError(
            "source_permission_request_scope_invalid",
            "The Source decision contains an invalid extraction scope.",
        )
    decision_scope_raw = decision.extraction_scope.get("locators", [])
    decision_scope = _string_list(decision_scope_raw)
    request_scope = _string_list(request.requested_scope_locators)
    if decision_scope is None or request_scope is None:
        raise SourcePermissionPolicyError(
            "source_permission_request_scope_invalid",
            "The Source decision contains an invalid extraction scope.",
        )
    if decision.extraction_permission == ExtractionPermission.SELECTED_SECTIONS:
        if not decision_scope:
            raise SourcePermissionPolicyError(
                "source_permission_request_scope_exceeded",
                "The Source decision exceeds the requested extraction scope.",
            )
        if (
            request.requested_extraction_permission
            == ExtractionPermission.SELECTED_SECTIONS
            and not {value.casefold() for value in decision_scope}.issubset(
                {value.casefold() for value in request_scope}
            )
        ):
            raise SourcePermissionPolicyError(
                "source_permission_request_scope_exceeded",
                "The Source decision exceeds the requested extraction scope.",
            )
    elif decision_scope:
        raise SourcePermissionPolicyError(
            "source_permission_request_scope_invalid",
            "Only selected-section decisions may contain scope locators.",
        )
    if not isinstance(version.access_metadata, dict):
        raise SourcePermissionPolicyError(
            "source_permission_request_policy_invalid",
            "The Source version has invalid access metadata.",
        )
    redistribution_allowed = bool(
        version.access_metadata.get("redistribution_allowed", False)
    )
    if (
        request.requested_storage_permission
        == StoragePermission.DERIVED_WITH_SHORT_EXCERPT
        or decision.storage_permission
        == StoragePermission.DERIVED_WITH_SHORT_EXCERPT
    ) and not redistribution_allowed:
        raise SourcePermissionPolicyError(
            "source_permission_request_redistribution_forbidden",
            "Short-excerpt storage lacks redistribution authority.",
        )
    if version.content_access in {
        ContentAccess.SEARCH_SNIPPET,
        ContentAccess.ABSTRACT,
    } and (
        decision.extraction_permission
        in {
            ExtractionPermission.SELECTED_SECTIONS,
            ExtractionPermission.FULL_TEXT,
        }
        or decision.storage_permission != StoragePermission.NONE
    ):
        raise SourcePermissionPolicyError(
            "source_permission_request_content_access_insufficient",
            "The Source content access cannot support claim-level downstream use.",
        )


def _legacy_integrity_valid(
    request: SourcePermissionRequest,
    version: SourceVersion,
) -> bool:
    if not isinstance(version.access_metadata, dict):
        return False
    extraction_raw = version.access_metadata.get(
        "requested_extraction_permission", ExtractionPermission.NONE.value
    )
    storage_raw = version.access_metadata.get(
        "requested_storage_permission", StoragePermission.NONE.value
    )
    try:
        expected_extraction = ExtractionPermission(str(extraction_raw))
    except ValueError:
        expected_extraction = ExtractionPermission.NONE
    try:
        expected_storage = StoragePermission(str(storage_raw))
    except ValueError:
        expected_storage = StoragePermission.NONE
    scope_raw = version.access_metadata.get("requested_scope_locators", [])
    expected_scope = scope_raw if isinstance(scope_raw, list) else []
    expected_checksum = hashlib.sha256(
        (
            "magicforge:legacy-source-permission-request:"
            f"{version.id}:{version.content_hash}"
        ).encode("utf-8")
    ).hexdigest()
    expected_id = UUID(
        hashlib.md5(
            f"magicforge:source-permission-request:{version.id}".encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
    )
    return bool(
        request.sequence_number == 1
        and request.supersedes_request_id is None
        and request.submitted_by_user_id == version.submitted_by_user_id
        and request.actor_role_snapshot == []
        and request.requested_extraction_permission == expected_extraction
        and request.requested_storage_permission == expected_storage
        and request.requested_scope_locators == expected_scope
        and request.rights_basis
        == "legacy_access_metadata_snapshot_only_no_grant"
        and request.rights_evidence
        == [
            f"source-version:{version.id}",
            f"content-sha256:{version.content_hash}",
        ]
        and request.reason
        == (
            "Backfilled from immutable SourceVersion access_metadata; "
            "no permission elevation."
        )
        and hmac.compare_digest(request.request_checksum, expected_checksum)
        and request.id == expected_id
    )


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        normalized.append(item.strip())
    return normalized


def _extraction_rank(value: ExtractionPermission) -> int:
    return {
        ExtractionPermission.NONE: 0,
        ExtractionPermission.METADATA_ONLY: 1,
        ExtractionPermission.SELECTED_SECTIONS: 2,
        ExtractionPermission.FULL_TEXT: 3,
    }[value]


def _storage_rank(value: StoragePermission) -> int:
    return {
        StoragePermission.NONE: 0,
        StoragePermission.DERIVED_KNOWLEDGE_ONLY: 1,
        StoragePermission.DERIVED_WITH_SHORT_EXCERPT: 2,
    }[value]


__all__ = [
    "LEGACY_SOURCE_PERMISSION_REQUEST_SCHEMA_VERSION",
    "SourcePermissionPolicyError",
    "permission_request_checksum_payload",
    "require_current_source_approval",
    "require_permission_request_chain_integrity",
    "require_permission_request_integrity",
]
