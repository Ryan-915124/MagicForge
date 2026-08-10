"""Auditable citation records for academic and practitioner sources.

Citation verification is evidence-derived.  ``CitationRecord.status`` remains
part of the wire/file compatibility shape, but callers cannot make a citation
verified merely by setting that field; :class:`CitationManager` derives it
from the immutable evidence records below.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research.models import SearchProvenance, SourceCategory


class _FrozenJsonDict(dict[str, Any]):
    """Serializable dict snapshot that rejects in-place mutation."""

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("verification resolver result is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class CitationStatus(StrEnum):
    UNVERIFIED = "unverified"
    INCOMPLETE = "incomplete"
    METADATA_VERIFIED = "metadata_verified"
    FULL_TEXT_VERIFIED = "full_text_verified"
    FAILED = "failed"


class PeerReviewStatus(StrEnum):
    UNKNOWN = "unknown"
    PEER_REVIEWED = "peer_reviewed"
    PREPRINT = "preprint"
    NOT_APPLICABLE = "not_applicable"


class VerificationScope(StrEnum):
    METADATA = "metadata"
    FULL_TEXT = "full_text"


class VerificationMethod(StrEnum):
    METADATA_RESOLVER = "metadata_resolver"
    CANONICAL_LOCATOR = "canonical_locator"
    FULL_TEXT_LOCATOR = "full_text_locator"
    MANUAL_REVIEW = "manual_review"
    LEGACY_MIGRATION = "legacy_migration"


class VerificationEvidence(BaseModel):
    """One content-addressed verification observation.

    The two legacy input names (``verifier`` and ``verification_source``) are
    accepted so existing Bootstrap artifacts remain readable.  They are
    normalized into the explicit trust-boundary fields before validation and
    are not emitted by new serializations.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: VerificationScope
    method: VerificationMethod
    resolver_result: dict[str, Any]
    verified_identifier: str = Field(min_length=1)
    checked_locator: str = Field(min_length=1)
    review_actor: str = Field(min_length=1)
    verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    evidence_checksum: str = ""
    notes: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_shape(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy_actor = data.pop("verifier", None)
        legacy_locator = data.pop("verification_source", None)
        if legacy_actor is None and legacy_locator is None:
            return data

        actor = str(legacy_actor or "legacy-verification-record").strip()
        locator = str(legacy_locator or "legacy:unknown").strip()
        lowered_actor = actor.casefold()
        notes = str(data.get("notes") or "")
        full_text = "exact-content" in lowered_actor or "full text" in notes.casefold()
        data.setdefault(
            "scope",
            VerificationScope.FULL_TEXT if full_text else VerificationScope.METADATA,
        )
        data.setdefault(
            "method",
            (
                VerificationMethod.FULL_TEXT_LOCATOR
                if full_text
                else VerificationMethod.METADATA_RESOLVER
                if "crossref" in lowered_actor or "resolver" in lowered_actor
                else VerificationMethod.LEGACY_MIGRATION
            ),
        )
        data.setdefault(
            "resolver_result",
            {
                "legacy_evidence": True,
                "verifier": actor,
                "verification_source": locator,
            },
        )
        data.setdefault("verified_identifier", locator)
        data.setdefault("checked_locator", locator)
        data.setdefault("review_actor", actor)
        return data

    @field_validator("verified_identifier", "checked_locator", "review_actor")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("verification identity fields must not be empty")
        return normalized

    @field_validator("notes")
    @classmethod
    def normalize_notes(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("verified_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("verification timestamp must include a timezone")
        return value.astimezone(UTC)

    @field_validator("resolver_result")
    @classmethod
    def require_json_result(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("resolver_result must not be empty")
        try:
            normalized = json.loads(
                json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("resolver_result must contain JSON-safe values") from exc
        return _freeze_json(normalized)

    @model_validator(mode="after")
    def assign_and_validate_checksum(self) -> "VerificationEvidence":
        checksum = self.calculate_checksum()
        if self.evidence_checksum and self.evidence_checksum.casefold() != checksum:
            raise ValueError("verification evidence checksum does not match its contents")
        object.__setattr__(self, "evidence_checksum", checksum)
        return self

    def calculate_checksum(self) -> str:
        payload = {
            "scope": self.scope.value,
            "method": self.method.value,
            "resolver_result": self.resolver_result,
            "verified_identifier": self.verified_identifier,
            "checked_locator": self.checked_locator,
            "review_actor": self.review_actor,
            "verified_at": self.verified_at.isoformat(),
            "notes": self.notes,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def verifier(self) -> str:
        """Compatibility accessor for pre-integrity call sites."""

        return self.review_actor

    @property
    def verification_source(self) -> str:
        """Compatibility accessor for pre-integrity call sites."""

        return self.checked_locator


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenJsonDict(
            {str(key): _freeze_json(nested) for key, nested in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(nested) for nested in value)
    return value


class CitationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = ""
    source_candidate_id: str
    source_category: SourceCategory
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = Field(default=None, ge=1800, le=2200)
    venue: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doi: str | None = None
    url: str
    accessed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    peer_review_status: PeerReviewStatus = PeerReviewStatus.UNKNOWN
    status: CitationStatus = CitationStatus.UNVERIFIED
    missing_fields: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    provenance: list[SearchProvenance] = Field(default_factory=list)
    verification_evidence: list[VerificationEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def assign_id(self) -> "CitationRecord":
        if not self.id:
            identity = self.doi or self.url.casefold() or self.title.casefold()
            self.id = str(uuid5(NAMESPACE_URL, f"magicforge:citation:{identity}"))
        return self
