"""Application-facing retrieval and authorized projection-writing contracts."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from knowledge.projections import IngestionReceipt, StorageManifest
from knowledge.governance import ClearanceRank, SensitiveInformationLevel


class KnowledgeSearchFilter(BaseModel):
    """Caller-selected domain filters; security fields are deliberately absent."""

    knowledge_types: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    ontology_paths: list[str] = Field(default_factory=list)
    knowledge_origins: list[str] = Field(default_factory=list)
    evidence_levels: list[str] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    relation_types: list[str] = Field(default_factory=list)


class RetrievalAuthorization(BaseModel):
    """Server-created authorization context, never populated from request payloads.

    Every field that grants access is explicit.  In particular there is no
    permissive constructor default that could turn a missing actor into a
    controlled-knowledge reader.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str = Field(min_length=1)
    clearance_rank: ClearanceRank
    allowed_sensitive_levels: tuple[SensitiveInformationLevel, ...] = Field(
        min_length=1
    )
    bootstrap_limited: bool = False
    break_glass: bool = False

    @field_validator("principal_id")
    @classmethod
    def _normalize_principal_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("retrieval principal must not be empty")
        return normalized

    @field_validator("allowed_sensitive_levels")
    @classmethod
    def _deduplicate_sensitive_levels(
        cls,
        value: tuple[SensitiveInformationLevel, ...],
    ) -> tuple[SensitiveInformationLevel, ...]:
        return tuple(dict.fromkeys(value))

    @model_validator(mode="after")
    def _validate_clearance_boundaries(self) -> "RetrievalAuthorization":
        levels = set(self.allowed_sensitive_levels)
        if (
            SensitiveInformationLevel.SECRET_METHOD in levels
            and self.clearance_rank < ClearanceRank.METHOD_DETAIL
        ):
            raise ValueError("secret-method access requires method-detail clearance")
        if (
            SensitiveInformationLevel.RESTRICTED in levels
            or self.clearance_rank >= ClearanceRank.OPERATIONAL_SECRET
        ) and not self.break_glass:
            raise ValueError("restricted retrieval requires explicit break-glass state")
        if self.bootstrap_limited and (
            self.break_glass
            or self.clearance_rank > ClearanceRank.GENERAL_PRINCIPLE
            or not levels.issubset(
                {
                    SensitiveInformationLevel.PUBLIC,
                    SensitiveInformationLevel.CONTROLLED,
                }
            )
        ):
            raise ValueError("Bootstrap anonymous retrieval must remain limited")
        return self


class RetrievalAuthorizationRequiredError(PermissionError):
    """Raised when a retrieval boundary is reached without server authorization."""


def require_retrieval_authorization(
    authorization: RetrievalAuthorization | None,
) -> RetrievalAuthorization:
    if authorization is None:
        raise RetrievalAuthorizationRequiredError(
            "explicit retrieval authorization is required"
        )
    return authorization


class SearchResult(BaseModel):
    text: str
    score: float
    payload: dict[str, object] = Field(default_factory=dict)


class KnowledgeRetriever(Protocol):
    def search_documents(
        self,
        query: str,
        limit: int = 5,
        filters: KnowledgeSearchFilter | None = None,
        *,
        authorization: RetrievalAuthorization,
    ) -> list[SearchResult]: ...


class ProjectionWriter(Protocol):
    def write_manifest(
        self,
        manifest: StorageManifest,
        *,
        actor: str,
    ) -> IngestionReceipt: ...
