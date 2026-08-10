"""HTTP/application contracts for Checkpoint 4 storage governance."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from knowledge.governance import SensitiveInformationLevel


STORAGE_WORKFLOW_SCHEMA_VERSION = "storage-workflow-0.1"
STORAGE_VALIDATION_RULE_VERSIONS = {
    "evidence_eligibility": "evidence-eligibility-0.2",
    "entity_validation": "entity-validation-0.2",
    "relationship_entailment": "relationship-entailment-0.2",
    "sensitivity_policy": "sensitivity-policy-0.1",
    "current_approval_reconciliation": "current-approval-reconciliation-0.2",
    "mapping_evidence_binding": "mapping-evidence-binding-0.1",
    "schema_compatibility": "storage-schema-0.1",
}


class PersistentProductionWriteCapability(BaseModel):
    """One deliberately narrow authorization for one immutable manifest.

    This is runtime configuration, not a client request.  The global writer
    switch must still be enabled independently; this fingerprint prevents that
    broad switch from authorizing a different manifest or collection.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_id: UUID
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_name: str = Field(min_length=1, max_length=255)
    point_count: int = Field(gt=0)

    @field_validator("collection_name")
    @classmethod
    def normalize_collection(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "bootstrap" in normalized.casefold():
            raise ValueError("Production write capability cannot target Bootstrap")
        return normalized


class ManifestBuildCommand(BaseModel):
    """Artifact identities only; callers cannot supply vector projections."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_id: UUID
    collection_name: str = Field(min_length=1, max_length=255)
    evidence_version_ids: list[UUID] = Field(default_factory=list)
    knowledge_node_version_ids: list[UUID] = Field(default_factory=list)
    relationship_assertion_ids: list[UUID] = Field(default_factory=list)
    authorized_sensitive_levels: list[SensitiveInformationLevel] = Field(
        default_factory=lambda: [
            SensitiveInformationLevel.PUBLIC,
            SensitiveInformationLevel.CONTROLLED,
        ]
    )

    @field_validator(
        "evidence_version_ids",
        "knowledge_node_version_ids",
        "relationship_assertion_ids",
    )
    @classmethod
    def unique_ids(cls, values: list[UUID]) -> list[UUID]:
        return list(dict.fromkeys(values))

    @field_validator("authorized_sensitive_levels")
    @classmethod
    def unique_levels(
        cls, values: list[SensitiveInformationLevel]
    ) -> list[SensitiveInformationLevel]:
        return list(dict.fromkeys(values))

    @field_validator("collection_name")
    @classmethod
    def production_collection_only(cls, value: str) -> str:
        normalized = value.strip()
        if "bootstrap" in normalized.casefold():
            raise ValueError("Bootstrap collections cannot be storage targets")
        return normalized

    @model_validator(mode="after")
    def require_artifacts(self) -> "ManifestBuildCommand":
        if not (
            self.evidence_version_ids
            or self.knowledge_node_version_ids
            or self.relationship_assertion_ids
        ):
            raise ValueError("at least one approved artifact version is required")
        if not self.authorized_sensitive_levels:
            raise ValueError("at least one sensitivity level is required")
        return self


class ManifestAuthorizationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reason: str = Field(min_length=1, max_length=4_000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return " ".join(value.split())


class ManifestIngestionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reason: str = Field(min_length=1, max_length=4_000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return " ".join(value.split())


class CorpusActivationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    runtime_scope: str = Field(default="production", min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=4_000)

    @field_validator("runtime_scope", "reason")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return " ".join(value.split())


class ManifestItemView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_type: str
    artifact_row_id: UUID
    artifact_domain_id: UUID
    artifact_version: int
    payload_checksum: str
    projection_checksum: str
    projection_point_id: UUID
    sensitivity: SensitiveInformationLevel


class StorageManifestView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    corpus_id: UUID
    manifest_hash: str
    collection_name: str
    schema_version: str
    projection_schema_version: str
    status: Literal["pending", "authorized", "ingested"]
    expected_point_count: int
    validation_rule_versions: dict[str, str]
    authorized_sensitive_levels: list[SensitiveInformationLevel]
    created_by_user_id: UUID
    created_at: datetime
    authorized_by_user_id: UUID | None = None
    authorized_at: datetime | None = None
    items: list[ManifestItemView] = Field(default_factory=list)


class StorageManifestSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    corpus_id: UUID
    manifest_hash: str
    collection_name: str
    schema_version: str
    projection_schema_version: str
    status: Literal["pending", "authorized", "ingested"]
    expected_point_count: int
    created_by_user_id: UUID
    created_at: datetime
    authorized_by_user_id: UUID | None = None
    authorized_at: datetime | None = None


class EligibleArtifactView(BaseModel):
    """Manifest-safe metadata for one currently eligible exact artifact row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_type: Literal["evidence_card", "knowledge_node", "relationship"]
    artifact_row_id: UUID
    artifact_domain_id: UUID
    artifact_version: int
    payload_checksum: str
    sensitivity: SensitiveInformationLevel
    subject: str
    supporting_evidence_version_ids: list[UUID] = Field(default_factory=list)
    approved_at: datetime


class IngestionResultView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID
    manifest_id: UUID
    corpus_id: UUID
    receipt_id: UUID
    collection_name: str
    point_count: int
    state: Literal["succeeded"] = "succeeded"


class CorpusVersionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    corpus_id: UUID
    manifest_id: UUID
    ingestion_receipt_id: UUID
    runtime_scope: str
    qdrant_collection: str
    schema_version: str
    projection_version: str
    vector_size: int
    vector_distance: str
    activation_state: Literal["staged", "active", "inactive"]
    created_at: datetime
    activated_at: datetime | None = None


class CorpusVersionsView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    items: list[CorpusVersionView] = Field(default_factory=list)


__all__ = [
    "CorpusActivationCommand",
    "CorpusVersionView",
    "CorpusVersionsView",
    "EligibleArtifactView",
    "IngestionResultView",
    "ManifestAuthorizationCommand",
    "ManifestBuildCommand",
    "ManifestIngestionCommand",
    "ManifestItemView",
    "PersistentProductionWriteCapability",
    "STORAGE_VALIDATION_RULE_VERSIONS",
    "STORAGE_WORKFLOW_SCHEMA_VERSION",
    "StorageManifestView",
    "StorageManifestSummaryView",
]
