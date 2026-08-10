"""Relational persistence for authorized Production storage workflows.

This module is intentionally separate from ``persistence.models`` so the
Checkpoint 4 migration can be integrated without coupling storage writes to
Bootstrap artifacts.  Importing it registers the tables on the shared Base;
it never creates tables or opens a connection.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from persistence.models import (
    Base,
    JSON_VALUE,
    AppendOnlyMutationError,
    _string_enum,
    utc_now,
)


class StorageManifestState(StrEnum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    INGESTED = "ingested"


class IngestionOperationState(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_CLEANED = "failed_cleaned"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class CorpusActivationState(StrEnum):
    STAGED = "staged"
    ACTIVE = "active"
    INACTIVE = "inactive"


class StorageManifestRecord(Base):
    __tablename__ = "storage_manifests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'authorized', 'ingested')",
            name="storage_manifest_state",
        ),
        CheckConstraint("length(manifest_hash) = 64", name="ck_storage_manifest_hash"),
        CheckConstraint("expected_point_count > 0", name="ck_storage_manifest_points"),
        CheckConstraint("row_version > 0", name="ck_storage_manifest_row_version"),
        UniqueConstraint("manifest_hash", name="uq_storage_manifest_hash"),
        Index("ix_storage_manifest_corpus_status", "corpus_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    corpus_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    projection_schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    collection_name: Mapped[str] = mapped_column(String(255), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    validation_rule_versions: Mapped[dict[str, str]] = mapped_column(
        JSON_VALUE, nullable=False
    )
    authorized_sensitive_levels: Mapped[list[str]] = mapped_column(
        JSON_VALUE, nullable=False
    )
    expected_point_count: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[StorageManifestState] = mapped_column(
        _string_enum(StorageManifestState, name="storage_manifest_state"),
        nullable=False,
        default=StorageManifestState.PENDING,
        server_default=StorageManifestState.PENDING.value,
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now,
        server_default=func.now()
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __mapper_args__ = {"version_id_col": row_version}


class StorageManifestItemRecord(Base):
    """Immutable exact artifact version and checksum included in a manifest."""

    __tablename__ = "storage_manifest_items"
    __table_args__ = (
        CheckConstraint("sequence_number > 0", name="ck_manifest_item_sequence"),
        CheckConstraint("artifact_version > 0", name="ck_manifest_item_version"),
        CheckConstraint("length(payload_checksum) = 64", name="ck_manifest_item_checksum"),
        CheckConstraint("length(projection_checksum) = 64", name="ck_manifest_item_projection_checksum"),
        UniqueConstraint("manifest_id", "sequence_number", name="uq_manifest_item_sequence"),
        UniqueConstraint(
            "manifest_id", "artifact_type", "artifact_row_id",
            name="uq_manifest_item_artifact",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    manifest_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("storage_manifests.id", ondelete="RESTRICT"), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(80), nullable=False)
    artifact_row_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_domain_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    artifact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    projection_point_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class ManifestAuthorizationRecord(Base):
    """Append-only human authorization over one immutable manifest hash."""

    __tablename__ = "manifest_authorizations"
    __table_args__ = (
        CheckConstraint("length(manifest_hash) = 64", name="ck_manifest_authorization_hash"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_manifest_authorization_reason"),
        UniqueConstraint("manifest_id", name="uq_manifest_authorization_once"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    manifest_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("storage_manifests.id", ondelete="RESTRICT"), nullable=False
    )
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    authorizer_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_role_snapshot: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    validation_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class IngestionOperationRecord(Base):
    """Mutable saga envelope; success/failure history is retained in AuditEvent."""

    __tablename__ = "ingestion_operations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('running', 'succeeded', 'failed_cleaned', 'reconciliation_required')",
            name="ingestion_operation_state",
        ),
        CheckConstraint("length(request_hash) = 64", name="ck_ingestion_request_hash"),
        UniqueConstraint(
            "actor_user_id", "idempotency_key", name="uq_ingestion_actor_idempotency"
        ),
        Index("ix_ingestion_manifest_state", "manifest_id", "state"),
        Index(
            "uq_ingestion_one_active_per_manifest",
            "manifest_id",
            unique=True,
            postgresql_where=text(
                "state IN ('running', 'reconciliation_required')"
            ),
            sqlite_where=text(
                "state IN ('running', 'reconciliation_required')"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    manifest_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("storage_manifests.id", ondelete="RESTRICT"), nullable=False
    )
    actor_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[IngestionOperationState] = mapped_column(
        _string_enum(IngestionOperationState, name="ingestion_operation_state"),
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    cleanup_outcome: Mapped[str | None] = mapped_column(String(80))
    error_code: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestionReceiptRecord(Base):
    __tablename__ = "ingestion_receipts"
    __table_args__ = (
        CheckConstraint("length(manifest_hash) = 64", name="ck_ingestion_receipt_hash"),
        CheckConstraint("length(payload_checksum) = 64", name="ck_ingestion_receipt_checksum"),
        UniqueConstraint("manifest_id", name="uq_ingestion_receipt_manifest"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    manifest_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("storage_manifests.id", ondelete="RESTRICT"), nullable=False
    )
    corpus_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    collection_name: Mapped[str] = mapped_column(String(255), nullable=False)
    receipt_payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ingestion_operations.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class CorpusVersionRecord(Base):
    __tablename__ = "corpus_versions"
    __table_args__ = (
        CheckConstraint(
            "activation_state IN ('staged', 'active', 'inactive')",
            name="corpus_activation_state",
        ),
        UniqueConstraint("manifest_id", name="uq_corpus_manifest"),
        Index("ix_corpus_scope_state", "runtime_scope", "activation_state"),
        Index(
            "uq_corpus_one_active_per_scope",
            "runtime_scope",
            unique=True,
            postgresql_where=text("activation_state = 'active'"),
            sqlite_where=text("activation_state = 'active'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    manifest_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("storage_manifests.id", ondelete="RESTRICT"), nullable=False
    )
    ingestion_receipt_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ingestion_receipts.id", ondelete="RESTRICT"), nullable=False
    )
    runtime_scope: Mapped[str] = mapped_column(String(120), nullable=False, default="production")
    qdrant_collection: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    projection_version: Mapped[str] = mapped_column(String(80), nullable=False)
    vector_size: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_distance: Mapped[str] = mapped_column(String(40), nullable=False)
    activation_state: Mapped[CorpusActivationState] = mapped_column(
        _string_enum(CorpusActivationState, name="corpus_activation_state"), nullable=False,
        default=CorpusActivationState.STAGED, server_default=CorpusActivationState.STAGED.value,
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActiveCorpusPointer(Base):
    __tablename__ = "active_corpus_pointers"
    __table_args__ = (
        CheckConstraint("row_version > 0", name="ck_active_corpus_row_version"),
    )

    runtime_scope: Mapped[str] = mapped_column(String(120), primary_key=True)
    corpus_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("corpus_versions.id", ondelete="RESTRICT"), nullable=False
    )
    activated_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __mapper_args__ = {"version_id_col": row_version}


_IMMUTABLE = (
    StorageManifestItemRecord,
    ManifestAuthorizationRecord,
    IngestionReceiptRecord,
)


def _reject_mutation(_mapper: Any, _connection: Any, target: Any) -> None:
    raise AppendOnlyMutationError(f"{target.__tablename__} records are immutable")


for _model in _IMMUTABLE:
    event.listen(_model, "before_update", _reject_mutation, propagate=True)
    event.listen(_model, "before_delete", _reject_mutation, propagate=True)


__all__ = [
    "ActiveCorpusPointer",
    "CorpusActivationState",
    "CorpusVersionRecord",
    "IngestionOperationRecord",
    "IngestionOperationState",
    "IngestionReceiptRecord",
    "ManifestAuthorizationRecord",
    "StorageManifestItemRecord",
    "StorageManifestRecord",
    "StorageManifestState",
]
