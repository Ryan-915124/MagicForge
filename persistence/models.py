"""Relational persistence models for MagicForge governance infrastructure.

The models in this module are deliberately infrastructure-only.  Bootstrap
artifacts remain file-backed and no table creation is performed on import.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    JSON,
    CheckConstraint,
    DateTime,
    Enum as SqlEnum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    inspect as sa_inspect,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Uuid

from knowledge.evidence import (
    ClaimPolarity,
    ClaimRole,
    ContradictionStatus,
    EvidenceClass,
    EvidenceLevel,
    KnowledgeOrigin,
    SourceType,
)
from knowledge.governance import (
    ClaimEligibility,
    ContradictionCheckStatus,
    ExtractionPermission,
    SecretExposureLevel,
    SensitiveInformationLevel,
    StoragePermission,
)
from research.citation.models import VerificationMethod, VerificationScope
from research.models import ContentAccess
from knowledge.models import EntityType, RelationType
from research.review.mapping_models import MappingProposalKind


def utc_now() -> datetime:
    """Return an aware UTC timestamp for ORM-side defaults."""

    return datetime.now(UTC)


JSON_VALUE = JSON(none_as_null=True).with_variant(
    JSONB(none_as_null=True), "postgresql"
)


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class RoleName(StrEnum):
    READER = "reader"
    REVIEWER = "reviewer"
    OPERATOR = "operator"
    ADMIN = "admin"


class SessionTransport(StrEnum):
    COOKIE = "cookie"
    BEARER = "bearer"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class IdempotencyStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class GovernanceWorkflowStatus(StrEnum):
    """Persisted lifecycle shared by Source and Claim review aggregates."""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class ReviewDecisionKind(StrEnum):
    """Append-only reviewer action; corrections never rewrite old decisions."""

    APPROVED = "approved"
    REJECTED = "rejected"
    CORRECTED = "corrected"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"


class CandidateCreatedByKind(StrEnum):
    HUMAN = "human"
    GLM = "glm"
    PIPELINE = "pipeline"


class MappingValidationPhase(StrEnum):
    REVIEW = "review"
    MANIFEST_AUTHORIZATION = "manifest_authorization"


class CanonicalEntityStatus(StrEnum):
    ACTIVE = "active"
    MERGED = "merged"


class ExtractionJobStatus(StrEnum):
    """Mutable orchestration state for one immutable extraction specification."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExtractionAttemptStatus(StrEnum):
    """Terminal outcome recorded by one append-only extraction attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _string_enum(enum_type: type[StrEnum], *, name: str) -> SqlEnum:
    return SqlEnum(
        enum_type,
        values_callable=lambda values: [item.value for item in values],
        native_enum=False,
        validate_strings=True,
        # Explicit table-level checks below keep Alembic autogeneration stable
        # across PostgreSQL and SQLite instead of relying on implicit Enum DDL.
        create_constraint=False,
        name=name,
    )


class Base(DeclarativeBase):
    """Base metadata used by Alembic; never passed to create_all in runtime."""


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'disabled')", name="user_status"
        ),
        CheckConstraint("row_version > 0", name="ck_users_row_version_positive"),
        Index("ix_users_status_created_at", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(80), nullable=False)
    username_normalized: Mapped[str] = mapped_column(
        String(80), nullable=False, unique=True
    )
    email: Mapped[str | None] = mapped_column(String(320))
    email_normalized: Mapped[str | None] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        _string_enum(UserStatus, name="user_status"),
        nullable=False,
        default=UserStatus.ACTIVE,
        server_default=UserStatus.ACTIVE.value,
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    __mapper_args__ = {"version_id_col": row_version}


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint(
            "name IN ('reader', 'reviewer', 'operator', 'admin')",
            name="role_name",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[RoleName] = mapped_column(
        _string_enum(RoleName, name="role_name"), nullable=False, unique=True
    )
    description: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_user_roles_revocation_order",
        ),
        CheckConstraint(
            "row_version > 0", name="ck_user_roles_row_version_positive"
        ),
        Index("ix_user_roles_role_revoked", "role_id", "revoked_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    role_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("roles.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    granted_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    __mapper_args__ = {"version_id_col": row_version}


class SessionRecord(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "transport IN ('cookie', 'bearer')", name="session_transport"
        ),
        CheckConstraint(
            "status IN ('active', 'revoked', 'expired')", name="session_status"
        ),
        CheckConstraint("length(token_hash) = 64", name="ck_sessions_token_hash"),
        CheckConstraint(
            "csrf_token_hash IS NULL OR length(csrf_token_hash) = 64",
            name="ck_sessions_csrf_token_hash",
        ),
        CheckConstraint("expires_at > created_at", name="ck_sessions_expiry_order"),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_sessions_revocation_order",
        ),
        CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status <> 'revoked' AND revoked_at IS NULL)",
            name="ck_sessions_status_revocation",
        ),
        CheckConstraint(
            "row_version > 0", name="ck_sessions_row_version_positive"
        ),
        Index("ix_sessions_user_expires", "user_id", "expires_at"),
        Index("ix_sessions_status_expires", "status", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    csrf_token_hash: Mapped[str | None] = mapped_column(String(64))
    transport: Mapped[SessionTransport] = mapped_column(
        _string_enum(SessionTransport, name="session_transport"), nullable=False
    )
    status: Mapped[SessionStatus] = mapped_column(
        _string_enum(SessionStatus, name="session_status"),
        nullable=False,
        default=SessionStatus.ACTIVE,
        server_default=SessionStatus.ACTIVE.value,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    revocation_reason: Mapped[str] = mapped_column(
        String(500), nullable=False, default="", server_default=""
    )
    client_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    __mapper_args__ = {"version_id_col": row_version}


class AuditEvent(Base):
    """Immutable governance event.

    The ORM rejects update/delete flushes.  The initial PostgreSQL migration
    additionally installs database-level protection so bulk SQL cannot bypass
    the invariant.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "length(trim(reason)) > 0", name="ck_audit_events_reason_present"
        ),
        CheckConstraint(
            "length(trim(correlation_id)) > 0",
            name="ck_audit_events_correlation_present",
        ),
        Index("ix_audit_events_actor_created", "actor_user_id", "created_at"),
        Index(
            "ix_audit_events_object_created",
            "object_type",
            "object_id",
            "created_at",
        ),
        Index("ix_audit_events_type_created", "event_type", "created_at"),
        Index("ix_audit_events_request_id", "request_id"),
        Index("ix_audit_events_correlation_id", "correlation_id"),
    )

    event_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_role_snapshot: Mapped[list[str]] = mapped_column(
        JSON_VALUE, nullable=False, default=list
    )
    object_type: Mapped[str] = mapped_column(String(120), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    domain_schema_version: Mapped[str] = mapped_column(
        String(80), nullable=False, default="audit-event-0.1", server_default="audit-event-0.1"
    )
    payload_checksum: Mapped[str | None] = mapped_column(String(64))
    previous_state: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(
        JSON_VALUE
    )
    new_state: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON_VALUE)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(255))
    correlation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_VALUE, nullable=False, default=dict
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed')",
            name="idempotency_status",
        ),
        UniqueConstraint(
            "actor_user_id",
            "scope",
            "idempotency_key",
            name="uq_idempotency_actor_scope_key",
        ),
        CheckConstraint(
            "length(request_hash) = 64", name="ck_idempotency_request_hash"
        ),
        CheckConstraint(
            "response_status_code IS NULL OR "
            "(response_status_code >= 100 AND response_status_code <= 599)",
            name="ck_idempotency_response_status",
        ),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="ck_idempotency_expiry_order",
        ),
        CheckConstraint(
            "row_version > 0", name="ck_idempotency_row_version_positive"
        ),
        Index("ix_idempotency_status_expires", "status", "expires_at"),
        Index("ix_idempotency_actor_scope", "actor_user_id", "scope"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    actor_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scope: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[IdempotencyStatus] = mapped_column(
        _string_enum(IdempotencyStatus, name="idempotency_status"),
        nullable=False,
        default=IdempotencyStatus.IN_PROGRESS,
        server_default=IdempotencyStatus.IN_PROGRESS.value,
    )
    response_status_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON_VALUE)
    error_code: Mapped[str | None] = mapped_column(String(120))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    __mapper_args__ = {"version_id_col": row_version}


class Source(Base):
    """Stable Source identity; mutable bibliographic/content data is versioned."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(
            "length(trim(canonical_key)) > 0",
            name="ck_sources_canonical_key_present",
        ),
        CheckConstraint("row_version > 0", name="ck_sources_row_version_positive"),
        Index("ix_sources_submitter_created", "submitted_by_user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    canonical_key: Mapped[str] = mapped_column(
        String(512), nullable=False, unique=True
    )
    submitted_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    __mapper_args__ = {"version_id_col": row_version}


class SourceVersion(Base):
    """Immutable snapshot of source content and its acquisition metadata."""

    __tablename__ = "source_versions"
    __table_args__ = (
        CheckConstraint(
            "content_access IN "
            "('search_snippet', 'abstract', 'web_extract', 'full_text', 'pdf_text')",
            name="source_version_content_access",
        ),
        CheckConstraint(
            "source_type IN "
            "('journal_article', 'conference_paper', 'preprint', "
            "'academic_book', 'book_chapter', 'practitioner_book', "
            "'web_article', 'interview', 'transcript', 'archival_material', "
            "'internal_analysis')",
            name="source_version_source_type",
        ),
        CheckConstraint(
            "knowledge_origin IN "
            "('scientific_evidence', 'expert_practice', 'personal_interpretation')",
            name="source_version_knowledge_origin",
        ),
        CheckConstraint(
            "sensitivity IN ('public', 'controlled', 'secret_method', 'restricted')",
            name="source_version_sensitivity",
        ),
        CheckConstraint(
            "version_number > 0", name="ck_source_versions_version_positive"
        ),
        CheckConstraint(
            "length(content_hash) = 64", name="ck_source_versions_content_hash"
        ),
        CheckConstraint(
            "length(trim(domain_schema_version)) > 0",
            name="ck_source_versions_schema_present",
        ),
        CheckConstraint(
            "supersedes_version_id IS NULL OR supersedes_version_id <> id",
            name="ck_source_versions_no_self_supersession",
        ),
        UniqueConstraint(
            "source_id", "version_number", name="uq_source_versions_source_version"
        ),
        UniqueConstraint(
            "source_id", "content_hash", name="uq_source_versions_source_hash"
        ),
        UniqueConstraint(
            "id", "content_hash", name="uq_source_versions_id_content_hash"
        ),
        Index("ix_source_versions_citation", "citation_id"),
        Index("ix_source_versions_hash", "content_hash"),
        Index("ix_source_versions_source_created", "source_id", "created_at"),
        Index(
            "ix_source_versions_submitter_created",
            "submitted_by_user_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    citation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    domain_schema_version: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="source-version-0.1",
        server_default="source-version-0.1",
    )
    citation_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    access_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_access: Mapped[ContentAccess] = mapped_column(
        _string_enum(ContentAccess, name="source_version_content_access"),
        nullable=False,
    )
    source_type: Mapped[SourceType] = mapped_column(
        _string_enum(SourceType, name="source_version_source_type"), nullable=False
    )
    knowledge_origin: Mapped[KnowledgeOrigin] = mapped_column(
        _string_enum(KnowledgeOrigin, name="source_version_knowledge_origin"),
        nullable=False,
    )
    sensitivity: Mapped[SensitiveInformationLevel] = mapped_column(
        _string_enum(
            SensitiveInformationLevel, name="source_version_sensitivity"
        ),
        nullable=False,
    )
    submitted_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    supersedes_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("source_versions.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class CitationVerificationEvidence(Base):
    """Immutable observation from which citation status is derived."""

    __tablename__ = "citation_verification_evidence"
    __table_args__ = (
        CheckConstraint(
            "verification_method IN "
            "('metadata_resolver', 'canonical_locator', 'full_text_locator', "
            "'manual_review', 'legacy_migration')",
            name="citation_verification_method",
        ),
        CheckConstraint(
            "verification_scope IN ('metadata', 'full_text')",
            name="citation_verification_scope",
        ),
        CheckConstraint(
            "length(evidence_checksum) = 64",
            name="ck_citation_verification_checksum",
        ),
        CheckConstraint(
            "length(trim(domain_schema_version)) > 0",
            name="ck_citation_verification_schema_present",
        ),
        CheckConstraint(
            "length(trim(verified_identifier)) > 0",
            name="ck_citation_verification_identifier_present",
        ),
        CheckConstraint(
            "length(trim(checked_locator)) > 0",
            name="ck_citation_verification_locator_present",
        ),
        UniqueConstraint(
            "source_version_id",
            "evidence_checksum",
            name="uq_citation_verification_source_checksum",
        ),
        Index(
            "ix_citation_verification_source_scope",
            "source_version_id",
            "verification_scope",
            "verified_at",
        ),
        Index("ix_citation_verification_citation", "citation_id"),
        Index(
            "ix_citation_verification_reviewer",
            "reviewer_user_id",
            "verified_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("source_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    citation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    verification_scope: Mapped[VerificationScope] = mapped_column(
        _string_enum(VerificationScope, name="citation_verification_scope"),
        nullable=False,
    )
    verification_method: Mapped[VerificationMethod] = mapped_column(
        _string_enum(VerificationMethod, name="citation_verification_method"),
        nullable=False,
    )
    resolver_result: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    verified_identifier: Mapped[str] = mapped_column(String(1024), nullable=False)
    checked_locator: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    evidence_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    domain_schema_version: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="citation-verification-evidence-0.1",
        server_default="citation-verification-evidence-0.1",
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class SourcePermissionRequest(Base):
    """Append-only rights request bound to one immutable Source version."""

    __tablename__ = "source_permission_requests"
    __table_args__ = (
        CheckConstraint(
            "requested_extraction_permission IN "
            "('none', 'metadata_only', 'selected_sections', 'full_text')",
            name="source_permission_request_extraction_permission",
        ),
        CheckConstraint(
            "requested_storage_permission IN "
            "('none', 'derived_knowledge_only', 'derived_with_short_excerpt')",
            name="source_permission_request_storage_permission",
        ),
        CheckConstraint(
            "sequence_number > 0",
            name="ck_source_permission_request_sequence_positive",
        ),
        CheckConstraint(
            "jsonb_typeof(requested_scope_locators) = 'array'",
            name="ck_source_permission_request_scope_array",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(trim(rights_basis)) > 0",
            name="ck_source_permission_request_rights_basis_present",
        ),
        CheckConstraint(
            "jsonb_typeof(rights_evidence) = 'array' AND "
            "jsonb_array_length(rights_evidence) > 0",
            name="ck_source_permission_request_rights_evidence_nonempty_array",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_source_permission_request_reason_present",
        ),
        CheckConstraint(
            "length(trim(domain_schema_version)) > 0",
            name="ck_source_permission_request_schema_present",
        ),
        CheckConstraint(
            "jsonb_typeof(actor_role_snapshot) = 'array'",
            name="ck_source_permission_request_actor_roles_array",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(request_checksum) = 64",
            name="ck_source_permission_request_checksum",
        ),
        CheckConstraint(
            "supersedes_request_id IS NULL OR supersedes_request_id <> id",
            name="ck_source_permission_request_no_self_supersession",
        ),
        UniqueConstraint(
            "source_version_id",
            "sequence_number",
            name="uq_source_permission_request_version_sequence",
        ),
        UniqueConstraint(
            "source_version_id",
            "request_checksum",
            name="uq_source_permission_request_version_checksum",
        ),
        UniqueConstraint(
            "id",
            "source_version_id",
            name="uq_source_permission_request_id_version",
        ),
        ForeignKeyConstraint(
            ["supersedes_request_id", "source_version_id"],
            [
                "source_permission_requests.id",
                "source_permission_requests.source_version_id",
            ],
            name="fk_source_permission_request_supersedes_version",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_source_permission_request_version_created",
            "source_version_id",
            "created_at",
        ),
        Index(
            "ix_source_permission_request_submitter_created",
            "submitted_by_user_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("source_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_extraction_permission: Mapped[ExtractionPermission] = mapped_column(
        _string_enum(
            ExtractionPermission,
            name="source_permission_request_extraction_permission",
        ),
        nullable=False,
    )
    requested_storage_permission: Mapped[StoragePermission] = mapped_column(
        _string_enum(
            StoragePermission,
            name="source_permission_request_storage_permission",
        ),
        nullable=False,
    )
    requested_scope_locators: Mapped[list[str]] = mapped_column(
        JSON_VALUE, nullable=False, default=list
    )
    rights_basis: Mapped[str] = mapped_column(Text, nullable=False)
    rights_evidence: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    submitted_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_role_snapshot: Mapped[list[str]] = mapped_column(
        JSON_VALUE, nullable=False, default=list
    )
    domain_schema_version: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="source-permission-request-0.1",
        server_default="source-permission-request-0.1",
    )
    request_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_request_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class SourceReviewDecision(Base):
    """One append-only Source review decision in a derived-state history."""

    __tablename__ = "source_review_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('approved', 'rejected', 'corrected', 'superseded', 'revoked')",
            name="source_review_decision_kind",
        ),
        CheckConstraint(
            "resulting_status IN ('approved', 'rejected', 'superseded', 'revoked')",
            name="source_review_resulting_status",
        ),
        CheckConstraint(
            "claim_eligibility IN "
            "('not_assessed', 'eligible', 'eligible_with_limits', 'ineligible')",
            name="source_review_claim_eligibility",
        ),
        CheckConstraint(
            "extraction_permission IN "
            "('none', 'metadata_only', 'selected_sections', 'full_text')",
            name="source_review_extraction_permission",
        ),
        CheckConstraint(
            "storage_permission IN "
            "('none', 'derived_knowledge_only', 'derived_with_short_excerpt')",
            name="source_review_storage_permission",
        ),
        CheckConstraint(
            "sensitive_information_level IN "
            "('public', 'controlled', 'secret_method', 'restricted')",
            name="source_review_sensitivity",
        ),
        CheckConstraint(
            "contradicting_evidence_checked IN "
            "('not_checked', 'checked_none_found', 'checked_conflicts_linked', "
            "'not_applicable')",
            name="source_review_contradiction_check",
        ),
        CheckConstraint(
            "sequence_number > 0", name="ck_source_review_sequence_positive"
        ),
        CheckConstraint(
            "length(trim(reason)) > 0", name="ck_source_review_reason_present"
        ),
        CheckConstraint(
            "length(decision_checksum) = 64",
            name="ck_source_review_decision_checksum",
        ),
        CheckConstraint(
            "supersedes_decision_id IS NULL OR supersedes_decision_id <> id",
            name="ck_source_review_no_self_supersession",
        ),
        CheckConstraint(
            "decision <> 'approved' OR resulting_status = 'approved'",
            name="ck_source_review_approved_result",
        ),
        CheckConstraint(
            "decision <> 'rejected' OR resulting_status = 'rejected'",
            name="ck_source_review_rejected_result",
        ),
        CheckConstraint(
            "decision <> 'superseded' OR resulting_status = 'superseded'",
            name="ck_source_review_superseded_result",
        ),
        CheckConstraint(
            "decision <> 'revoked' OR resulting_status = 'revoked'",
            name="ck_source_review_revoked_result",
        ),
        CheckConstraint(
            "resulting_status <> 'approved' OR "
            "(source_permission_request_id IS NOT NULL AND "
            "citation_verification_evidence_id IS NOT NULL AND "
            "claim_eligibility IN ('eligible', 'eligible_with_limits') AND "
            "extraction_permission IN ('selected_sections', 'full_text'))",
            name="ck_source_review_approval_requirements",
        ),
        ForeignKeyConstraint(
            ["source_permission_request_id", "source_version_id"],
            [
                "source_permission_requests.id",
                "source_permission_requests.source_version_id",
            ],
            name="fk_source_review_permission_request_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "source_version_id",
            "sequence_number",
            name="uq_source_review_version_sequence",
        ),
        UniqueConstraint(
            "source_version_id",
            "decision_checksum",
            name="uq_source_review_version_checksum",
        ),
        UniqueConstraint(
            "id",
            "source_version_id",
            "source_permission_request_id",
            name="uq_source_review_id_version_permission",
        ),
        Index(
            "ix_source_review_version_created",
            "source_version_id",
            "created_at",
        ),
        Index("ix_source_review_reviewer_created", "reviewer_user_id", "created_at"),
        Index("ix_source_review_result_created", "resulting_status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("source_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_permission_request_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[ReviewDecisionKind] = mapped_column(
        _string_enum(ReviewDecisionKind, name="source_review_decision_kind"),
        nullable=False,
    )
    resulting_status: Mapped[GovernanceWorkflowStatus] = mapped_column(
        _string_enum(
            GovernanceWorkflowStatus, name="source_review_resulting_status"
        ),
        nullable=False,
    )
    reviewer_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_role_snapshot: Mapped[list[str]] = mapped_column(
        JSON_VALUE, nullable=False, default=list
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    citation_verification_evidence_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("citation_verification_evidence.id", ondelete="RESTRICT"),
    )
    claim_eligibility: Mapped[ClaimEligibility] = mapped_column(
        _string_enum(ClaimEligibility, name="source_review_claim_eligibility"),
        nullable=False,
    )
    extraction_permission: Mapped[ExtractionPermission] = mapped_column(
        _string_enum(
            ExtractionPermission, name="source_review_extraction_permission"
        ),
        nullable=False,
    )
    storage_permission: Mapped[StoragePermission] = mapped_column(
        _string_enum(StoragePermission, name="source_review_storage_permission"),
        nullable=False,
    )
    sensitive_information_level: Mapped[SensitiveInformationLevel] = mapped_column(
        _string_enum(
            SensitiveInformationLevel, name="source_review_sensitivity"
        ),
        nullable=False,
    )
    contradicting_evidence_checked: Mapped[ContradictionCheckStatus] = mapped_column(
        _string_enum(
            ContradictionCheckStatus, name="source_review_contradiction_check"
        ),
        nullable=False,
    )
    extraction_scope: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    domain_schema_version: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="source-review-decision-0.1",
        server_default="source-review-decision-0.1",
    )
    decision_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("source_review_decisions.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class ExtractionJob(Base):
    """Governed, reproducible extraction specification and orchestration state.

    The approved Source chain and all configuration inputs are immutable. Only
    the lifecycle envelope changes as append-only attempts are recorded.
    """

    __tablename__ = "extraction_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="extraction_job_status",
        ),
        CheckConstraint(
            "sensitivity IN ('public', 'controlled', 'secret_method', 'restricted')",
            name="extraction_job_sensitivity",
        ),
        CheckConstraint("llm_provider = 'GLM'", name="ck_extraction_jobs_glm_only"),
        CheckConstraint(
            "length(content_hash) = 64",
            name="ck_extraction_jobs_content_hash",
        ),
        CheckConstraint(
            "length(configuration_checksum) = 64",
            name="ck_extraction_jobs_configuration_checksum",
        ),
        CheckConstraint(
            "length(idempotency_key) = 64",
            name="ck_extraction_jobs_idempotency_key",
        ),
        CheckConstraint(
            "result_checksum IS NULL OR length(result_checksum) = 64",
            name="ck_extraction_jobs_result_checksum",
        ),
        CheckConstraint(
            "error_code IS NULL OR (length(trim(error_code)) > 0 AND length(error_code) <= 120)",
            name="ck_extraction_jobs_error_code",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,119}$'",
            name="ck_extraction_jobs_error_code_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(trim(model_name)) > 0",
            name="ck_extraction_jobs_model_name_present",
        ),
        CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_extraction_jobs_prompt_version_present",
        ),
        CheckConstraint(
            "length(trim(output_schema_version)) > 0",
            name="ck_extraction_jobs_schema_version_present",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_extraction_jobs_attempt_count_nonnegative",
        ),
        CheckConstraint(
            "row_version > 0",
            name="ck_extraction_jobs_row_version_positive",
        ),
        CheckConstraint(
            "jsonb_typeof(scope_snapshot) = 'object'",
            name="ck_extraction_jobs_scope_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(model_snapshot) = 'object'",
            name="ck_extraction_jobs_model_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(prompt_snapshot) = 'object'",
            name="ck_extraction_jobs_prompt_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(schema_snapshot) = 'object'",
            name="ck_extraction_jobs_schema_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "jsonb_typeof(chunk_config_snapshot) = 'object'",
            name="ck_extraction_jobs_chunk_config_object",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "(status = 'queued' AND attempt_count = 0 AND started_at IS NULL "
            "AND finished_at IS NULL AND result_checksum IS NULL AND error_code IS NULL) OR "
            "(status = 'running' AND attempt_count > 0 AND started_at IS NOT NULL "
            "AND finished_at IS NULL AND result_checksum IS NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND attempt_count > 0 AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND result_checksum IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND attempt_count > 0 AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND result_checksum IS NULL AND error_code IS NOT NULL)",
            name="ck_extraction_jobs_state_payload",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_extraction_jobs_time_order",
        ),
        ForeignKeyConstraint(
            ["source_version_id", "content_hash"],
            ["source_versions.id", "source_versions.content_hash"],
            name="fk_extraction_job_source_version_hash",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "source_review_decision_id",
                "source_version_id",
                "source_permission_request_id",
            ],
            [
                "source_review_decisions.id",
                "source_review_decisions.source_version_id",
                "source_review_decisions.source_permission_request_id",
            ],
            name="fk_extraction_job_approved_source_chain",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("idempotency_key", name="uq_extraction_jobs_idempotency_key"),
        UniqueConstraint(
            "id", "source_version_id", name="uq_extraction_jobs_id_source_version"
        ),
        Index("ix_extraction_jobs_source_status", "source_version_id", "status"),
        Index("ix_extraction_jobs_review_created", "source_review_decision_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_review_decision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_permission_request_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    sensitivity: Mapped[SensitiveInformationLevel] = mapped_column(
        _string_enum(SensitiveInformationLevel, name="extraction_job_sensitivity"),
        nullable=False,
    )
    llm_provider: Mapped[str] = mapped_column(
        String(16), nullable=False, default="GLM", server_default="GLM"
    )
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    model_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    output_schema_version: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    chunk_config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    configuration_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ExtractionJobStatus] = mapped_column(
        _string_enum(ExtractionJobStatus, name="extraction_job_status"),
        nullable=False,
        default=ExtractionJobStatus.QUEUED,
        server_default=ExtractionJobStatus.QUEUED.value,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    result_checksum: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(120))
    requested_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now,
        server_default=func.now()
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __mapper_args__ = {"version_id_col": row_version}


class ExtractionAttempt(Base):
    """Append-only terminal outcome for one numbered extraction attempt."""

    __tablename__ = "extraction_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('succeeded', 'failed')",
            name="extraction_attempt_status",
        ),
        CheckConstraint("attempt_number > 0", name="ck_extraction_attempts_number_positive"),
        CheckConstraint(
            "result_checksum IS NULL OR length(result_checksum) = 64",
            name="ck_extraction_attempts_result_checksum",
        ),
        CheckConstraint(
            "length(attempt_checksum) = 64",
            name="ck_extraction_attempts_checksum",
        ),
        CheckConstraint(
            "error_code IS NULL OR (length(trim(error_code)) > 0 AND length(error_code) <= 120)",
            name="ck_extraction_attempts_error_code",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,119}$'",
            name="ck_extraction_attempts_error_code_format",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "finished_at >= started_at",
            name="ck_extraction_attempts_time_order",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND result_checksum IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND result_checksum IS NULL AND error_code IS NOT NULL)",
            name="ck_extraction_attempts_outcome",
        ),
        UniqueConstraint(
            "extraction_job_id", "attempt_number", name="uq_extraction_attempts_job_number"
        ),
        UniqueConstraint(
            "extraction_job_id", "attempt_checksum", name="uq_extraction_attempts_job_checksum"
        ),
        Index("ix_extraction_attempts_job_finished", "extraction_job_id", "finished_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    extraction_job_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("extraction_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ExtractionAttemptStatus] = mapped_column(
        _string_enum(ExtractionAttemptStatus, name="extraction_attempt_status"),
        nullable=False,
    )
    result_checksum: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(120))
    attempt_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class ExtractionProposalArtifact(Base):
    """Append-only sanitized proposal envelope retained for downstream review."""

    __tablename__ = "extraction_proposal_artifacts"
    __table_args__ = (
        CheckConstraint(
            "length(trim(extraction_run_id)) > 0",
            name="ck_extraction_proposals_run_id_present",
        ),
        CheckConstraint(
            "attempt_number > 0",
            name="ck_extraction_proposals_attempt_positive",
        ),
        CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_extraction_proposals_schema_present",
        ),
        CheckConstraint(
            "length(payload_checksum) = 64",
            name="ck_extraction_proposals_checksum",
        ),
        CheckConstraint(
            "sensitivity IN ('public', 'controlled', 'secret_method', 'restricted')",
            name="extraction_proposal_sensitivity",
        ),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_extraction_proposals_payload_object",
        ).ddl_if(dialect="postgresql"),
        ForeignKeyConstraint(
            ["extraction_job_id", "source_version_id"],
            ["extraction_jobs.id", "extraction_jobs.source_version_id"],
            name="fk_extraction_proposal_job_source",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "extraction_job_id", name="uq_extraction_proposals_job"
        ),
        UniqueConstraint(
            "proposal_id", name="uq_extraction_proposals_proposal_id"
        ),
        Index("ix_extraction_proposals_source_created", "source_version_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    extraction_job_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    source_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    proposal_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    extraction_run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    sensitivity: Mapped[SensitiveInformationLevel] = mapped_column(
        _string_enum(
            SensitiveInformationLevel, name="extraction_proposal_sensitivity"
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class ClaimCandidate(Base):
    """Mutable workflow envelope around an immutable, checksummed proposal."""

    __tablename__ = "claim_candidates"
    __table_args__ = (
        CheckConstraint(
            "claim_role IN "
            "('result', 'method', 'background', 'hypothesis', 'discussion', "
            "'expert_opinion', 'context_only')",
            name="claim_candidate_role",
        ),
        CheckConstraint(
            "claim_polarity IN ('supports', 'contradicts', 'qualifies')",
            name="claim_candidate_polarity",
        ),
        CheckConstraint(
            "created_by_kind IN ('human', 'glm', 'pipeline')",
            name="claim_candidate_created_by_kind",
        ),
        CheckConstraint(
            "status IN "
            "('draft', 'submitted', 'under_review', 'approved', 'rejected', "
            "'superseded', 'revoked')",
            name="claim_candidate_workflow_status",
        ),
        CheckConstraint(
            "length(trim(claim_text)) > 0",
            name="ck_claim_candidates_claim_present",
        ),
        CheckConstraint(
            "length(trim(candidate_schema_version)) > 0",
            name="ck_claim_candidates_schema_present",
        ),
        CheckConstraint(
            "length(candidate_checksum) = 64",
            name="ck_claim_candidates_checksum",
        ),
        CheckConstraint(
            "extraction_confidence >= 0.0 AND extraction_confidence <= 1.0",
            name="ck_claim_candidates_extraction_confidence",
        ),
        CheckConstraint(
            "claim_role <> 'context_only' OR evidence_card_eligible = false",
            name="ck_claim_candidates_context_not_evidence_eligible",
        ),
        CheckConstraint(
            "supersedes_candidate_id IS NULL OR supersedes_candidate_id <> id",
            name="ck_claim_candidates_no_self_supersession",
        ),
        CheckConstraint(
            "row_version > 0", name="ck_claim_candidates_row_version_positive"
        ),
        UniqueConstraint(
            "source_version_id",
            "candidate_checksum",
            name="uq_claim_candidates_source_checksum",
        ),
        Index("ix_claim_candidates_source_status", "source_version_id", "status"),
        Index("ix_claim_candidates_status_created", "status", "created_at"),
        Index(
            "ix_claim_candidates_submitter_status",
            "submitted_by_user_id",
            "status",
        ),
        Index("ix_claim_candidates_canonical_claim", "canonical_claim_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("source_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    canonical_claim_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_role: Mapped[ClaimRole] = mapped_column(
        _string_enum(ClaimRole, name="claim_candidate_role"), nullable=False
    )
    claim_polarity: Mapped[ClaimPolarity] = mapped_column(
        _string_enum(ClaimPolarity, name="claim_candidate_polarity"),
        nullable=False,
        default=ClaimPolarity.SUPPORTS,
        server_default=ClaimPolarity.SUPPORTS.value,
    )
    locator: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    extraction_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    model_run_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    proposed_evidence_card: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE, nullable=False, default=dict
    )
    candidate_schema_version: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="claim-candidate-0.1",
        server_default="claim-candidate-0.1",
    )
    candidate_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_card_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    status: Mapped[GovernanceWorkflowStatus] = mapped_column(
        _string_enum(
            GovernanceWorkflowStatus, name="claim_candidate_workflow_status"
        ),
        nullable=False,
        default=GovernanceWorkflowStatus.DRAFT,
        server_default=GovernanceWorkflowStatus.DRAFT.value,
    )
    submitted_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_by_kind: Mapped[CandidateCreatedByKind] = mapped_column(
        _string_enum(
            CandidateCreatedByKind, name="claim_candidate_created_by_kind"
        ),
        nullable=False,
    )
    supersedes_candidate_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("claim_candidates.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    __mapper_args__ = {"version_id_col": row_version}


class ClaimReviewDecision(Base):
    """Append-only human decision over exactly one Claim candidate."""

    __tablename__ = "claim_review_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('approved', 'rejected', 'corrected', 'superseded', 'revoked')",
            name="claim_review_decision_kind",
        ),
        CheckConstraint(
            "resulting_status IN ('approved', 'rejected', 'superseded', 'revoked')",
            name="claim_review_resulting_status",
        ),
        CheckConstraint(
            "evidence_class IS NULL OR evidence_class IN "
            "('controlled_experiment', 'quasi_experiment', 'observational_study', "
            "'systematic_review', 'meta_analysis', 'narrative_review', "
            "'expert_instruction', 'expert_case_analysis', 'practitioner_report', "
            "'historical_primary_record', 'historical_secondary_analysis', "
            "'analyst_interpretation', 'anecdotal_observation')",
            name="claim_review_evidence_class",
        ),
        CheckConstraint(
            "knowledge_origin IS NULL OR knowledge_origin IN "
            "('scientific_evidence', 'expert_practice', 'personal_interpretation')",
            name="claim_review_knowledge_origin",
        ),
        CheckConstraint(
            "evidence_level IS NULL OR evidence_level IN "
            "('empirical', 'review', 'practitioner', 'anecdotal')",
            name="claim_review_evidence_level",
        ),
        CheckConstraint(
            "claim_eligibility IN "
            "('not_assessed', 'eligible', 'eligible_with_limits', 'ineligible')",
            name="claim_review_claim_eligibility",
        ),
        CheckConstraint(
            "storage_permission IN "
            "('none', 'derived_knowledge_only', 'derived_with_short_excerpt')",
            name="claim_review_storage_permission",
        ),
        CheckConstraint(
            "contradiction_status IN "
            "('not_checked', 'none_found', 'resolved', 'unresolved')",
            name="claim_review_contradiction_status",
        ),
        CheckConstraint(
            "contradiction_check_status IN "
            "('not_checked', 'checked_none_found', 'checked_conflicts_linked', "
            "'not_applicable')",
            name="claim_review_contradiction_check",
        ),
        CheckConstraint(
            "sensitive_information_level IN "
            "('public', 'controlled', 'secret_method', 'restricted')",
            name="claim_review_sensitivity",
        ),
        CheckConstraint(
            "secret_exposure_level IN "
            "('none', 'general_principle', 'method_detail', 'operational_secret')",
            name="claim_review_secret_exposure",
        ),
        CheckConstraint(
            "sequence_number > 0", name="ck_claim_review_sequence_positive"
        ),
        CheckConstraint(
            "length(trim(reason)) > 0", name="ck_claim_review_reason_present"
        ),
        CheckConstraint(
            "length(decision_checksum) = 64",
            name="ck_claim_review_decision_checksum",
        ),
        CheckConstraint(
            "supersedes_decision_id IS NULL OR supersedes_decision_id <> id",
            name="ck_claim_review_no_self_supersession",
        ),
        CheckConstraint(
            "decision <> 'approved' OR resulting_status = 'approved'",
            name="ck_claim_review_approved_result",
        ),
        CheckConstraint(
            "decision <> 'rejected' OR resulting_status = 'rejected'",
            name="ck_claim_review_rejected_result",
        ),
        CheckConstraint(
            "decision <> 'superseded' OR resulting_status = 'superseded'",
            name="ck_claim_review_superseded_result",
        ),
        CheckConstraint(
            "decision <> 'revoked' OR resulting_status = 'revoked'",
            name="ck_claim_review_revoked_result",
        ),
        CheckConstraint(
            "resulting_status <> 'approved' OR "
            "(confidence IS NOT NULL AND evidence_class IS NOT NULL AND "
            "knowledge_origin IS NOT NULL AND evidence_level IS NOT NULL AND "
            "claim_eligibility IN ('eligible', 'eligible_with_limits') AND "
            "storage_permission IN "
            "('derived_knowledge_only', 'derived_with_short_excerpt') AND "
            "contradiction_status <> 'not_checked' AND "
            "contradiction_check_status <> 'not_checked')",
            name="ck_claim_review_approval_requirements",
        ),
        UniqueConstraint(
            "claim_candidate_id",
            "sequence_number",
            name="uq_claim_review_candidate_sequence",
        ),
        UniqueConstraint(
            "claim_candidate_id",
            "decision_checksum",
            name="uq_claim_review_candidate_checksum",
        ),
        Index(
            "ix_claim_review_candidate_created", "claim_candidate_id", "created_at"
        ),
        Index("ix_claim_review_reviewer_created", "reviewer_user_id", "created_at"),
        Index("ix_claim_review_result_created", "resulting_status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    claim_candidate_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("claim_candidates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[ReviewDecisionKind] = mapped_column(
        _string_enum(ReviewDecisionKind, name="claim_review_decision_kind"),
        nullable=False,
    )
    resulting_status: Mapped[GovernanceWorkflowStatus] = mapped_column(
        _string_enum(
            GovernanceWorkflowStatus, name="claim_review_resulting_status"
        ),
        nullable=False,
    )
    reviewer_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    actor_role_snapshot: Mapped[list[str]] = mapped_column(
        JSON_VALUE, nullable=False, default=list
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    limitations: Mapped[list[str]] = mapped_column(
        JSON_VALUE, nullable=False, default=list
    )
    evidence_class: Mapped[EvidenceClass | None] = mapped_column(
        _string_enum(EvidenceClass, name="claim_review_evidence_class")
    )
    knowledge_origin: Mapped[KnowledgeOrigin | None] = mapped_column(
        _string_enum(KnowledgeOrigin, name="claim_review_knowledge_origin")
    )
    evidence_level: Mapped[EvidenceLevel | None] = mapped_column(
        _string_enum(EvidenceLevel, name="claim_review_evidence_level")
    )
    claim_eligibility: Mapped[ClaimEligibility] = mapped_column(
        _string_enum(ClaimEligibility, name="claim_review_claim_eligibility"),
        nullable=False,
    )
    storage_permission: Mapped[StoragePermission] = mapped_column(
        _string_enum(StoragePermission, name="claim_review_storage_permission"),
        nullable=False,
    )
    contradiction_status: Mapped[ContradictionStatus] = mapped_column(
        _string_enum(ContradictionStatus, name="claim_review_contradiction_status"),
        nullable=False,
    )
    contradiction_check_status: Mapped[ContradictionCheckStatus] = mapped_column(
        _string_enum(
            ContradictionCheckStatus, name="claim_review_contradiction_check"
        ),
        nullable=False,
    )
    contradicting_evidence_ids: Mapped[list[str]] = mapped_column(
        JSON_VALUE, nullable=False, default=list
    )
    sensitive_information_level: Mapped[SensitiveInformationLevel] = mapped_column(
        _string_enum(SensitiveInformationLevel, name="claim_review_sensitivity"),
        nullable=False,
    )
    secret_exposure_level: Mapped[SecretExposureLevel] = mapped_column(
        _string_enum(SecretExposureLevel, name="claim_review_secret_exposure"),
        nullable=False,
    )
    policy_schema_version: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="claim-review-decision-0.1",
        server_default="claim-review-decision-0.1",
    )
    decision_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("claim_review_decisions.id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class EvidenceCardVersion(Base):
    """Immutable persisted projection of one approved EvidenceCard payload."""

    __tablename__ = "evidence_card_versions"
    __table_args__ = (
        CheckConstraint(
            "version_number > 0", name="ck_evidence_card_versions_version_positive"
        ),
        CheckConstraint(
            "length(trim(domain_schema_version)) > 0",
            name="ck_evidence_card_versions_schema_present",
        ),
        CheckConstraint(
            "length(payload_checksum) = 64",
            name="ck_evidence_card_versions_checksum",
        ),
        CheckConstraint(
            "supersedes_version_id IS NULL OR supersedes_version_id <> id",
            name="ck_evidence_card_versions_no_self_supersession",
        ),
        UniqueConstraint(
            "evidence_card_id",
            "version_number",
            name="uq_evidence_card_versions_family_version",
        ),
        UniqueConstraint(
            "domain_object_id", name="uq_evidence_card_versions_domain_object"
        ),
        UniqueConstraint(
            "claim_review_decision_id",
            name="uq_evidence_card_versions_review_decision",
        ),
        Index(
            "ix_evidence_card_versions_claim_created",
            "claim_candidate_id",
            "created_at",
        ),
        Index(
            "ix_evidence_card_versions_source_version",
            "source_version_id",
            "created_at",
        ),
        Index("ix_evidence_card_versions_citation", "citation_id"),
        Index("ix_evidence_card_versions_canonical_claim", "canonical_claim_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    evidence_card_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    domain_object_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_candidate_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("claim_candidates.id", ondelete="RESTRICT"),
        nullable=False,
    )
    claim_review_decision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("claim_review_decisions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("source_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    citation_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    canonical_claim_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    domain_schema_version: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        default="evidence-0.2",
        server_default="evidence-0.2",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("evidence_card_versions.id", ondelete="RESTRICT"),
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class MappingProposalRecord(Base):
    """Immutable Mapping payload wrapped by a decision-derived status."""

    __tablename__ = "mapping_proposals"
    __table_args__ = (
        CheckConstraint(
            "proposal_kind IN ('entity', 'relationship')",
            name="mapping_proposal_kind",
        ),
        CheckConstraint(
            "status IN ('submitted', 'approved', 'rejected', 'superseded', 'revoked')",
            name="mapping_proposal_workflow_status",
        ),
        CheckConstraint(
            "sensitivity IN ('public', 'controlled', 'secret_method', 'restricted')",
            name="mapping_proposal_sensitivity",
        ),
        CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_mapping_proposals_schema_present",
        ),
        CheckConstraint(
            "length(trim(validation_rule_version)) > 0",
            name="ck_mapping_proposals_rule_present",
        ),
        CheckConstraint(
            "length(proposal_checksum) = 64",
            name="ck_mapping_proposals_checksum",
        ),
        CheckConstraint(
            "proposer_user_id IS NOT NULL OR "
            "(proposer_run_id IS NOT NULL AND length(trim(proposer_run_id)) > 0)",
            name="ck_mapping_proposals_proposer_present",
        ),
        CheckConstraint(
            "supersedes_proposal_id IS NULL OR supersedes_proposal_id <> id",
            name="ck_mapping_proposals_no_self_supersession",
        ),
        CheckConstraint(
            "row_version > 0", name="ck_mapping_proposals_row_version_positive"
        ),
        UniqueConstraint(
            "proposal_kind", "proposal_checksum", name="uq_mapping_proposals_kind_checksum"
        ),
        Index("ix_mapping_proposals_status_submitted", "status", "submitted_at"),
        Index("ix_mapping_proposals_proposer_status", "proposer_user_id", "status"),
        Index("ix_mapping_proposals_subject", "subject_entity_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    proposal_kind: Mapped[MappingProposalKind] = mapped_column(
        _string_enum(MappingProposalKind, name="mapping_proposal_kind"), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    validation_rule_version: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    proposal_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_entity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    sensitivity: Mapped[SensitiveInformationLevel] = mapped_column(
        _string_enum(SensitiveInformationLevel, name="mapping_proposal_sensitivity"),
        nullable=False,
    )
    proposer_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    proposer_run_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[GovernanceWorkflowStatus] = mapped_column(
        _string_enum(
            GovernanceWorkflowStatus, name="mapping_proposal_workflow_status"
        ),
        nullable=False,
        default=GovernanceWorkflowStatus.SUBMITTED,
        server_default=GovernanceWorkflowStatus.SUBMITTED.value,
    )
    approved_artifact_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    approved_artifact_version: Mapped[int | None] = mapped_column(Integer)
    supersedes_proposal_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mapping_proposals.id", ondelete="RESTRICT")
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
    row_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    __mapper_args__ = {"version_id_col": row_version}


class MappingProposalEvidence(Base):
    """Exact EvidenceCardVersion references used by a Mapping proposal."""

    __tablename__ = "mapping_proposal_evidence"
    __table_args__ = (
        CheckConstraint(
            "length(evidence_payload_checksum) = 64",
            name="ck_mapping_proposal_evidence_checksum",
        ),
        Index("ix_mapping_proposal_evidence_version", "evidence_card_version_id"),
    )

    mapping_proposal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("mapping_proposals.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    evidence_card_version_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("evidence_card_versions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    evidence_domain_object_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    evidence_payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class MappingValidationRun(Base):
    """Append-only result of rerunning a versioned deterministic gate bundle."""

    __tablename__ = "mapping_validation_runs"
    __table_args__ = (
        CheckConstraint(
            "phase IN ('review', 'manifest_authorization')",
            name="mapping_validation_phase",
        ),
        CheckConstraint(
            "length(trim(rule_version)) > 0",
            name="ck_mapping_validation_rule_present",
        ),
        CheckConstraint(
            "length(input_checksum) = 64",
            name="ck_mapping_validation_input_checksum",
        ),
        CheckConstraint(
            "length(run_checksum) = 64",
            name="ck_mapping_validation_run_checksum",
        ),
        Index("ix_mapping_validation_proposal_phase", "mapping_proposal_id", "phase"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    mapping_proposal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mapping_proposals.id", ondelete="RESTRICT"), nullable=False
    )
    phase: Mapped[MappingValidationPhase] = mapped_column(
        _string_enum(MappingValidationPhase, name="mapping_validation_phase"),
        nullable=False,
    )
    rule_version: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_versions: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    input_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    results: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    run_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class MappingReviewDecision(Base):
    """Append-only reviewer decision over one Mapping proposal."""

    __tablename__ = "mapping_review_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('approved', 'rejected', 'corrected', 'superseded', 'revoked')",
            name="mapping_review_decision_kind",
        ),
        CheckConstraint(
            "resulting_status IN ('approved', 'rejected', 'superseded', 'revoked')",
            name="mapping_review_resulting_status",
        ),
        CheckConstraint("sequence_number > 0", name="ck_mapping_review_sequence_positive"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_mapping_review_reason_present"),
        CheckConstraint("length(decision_checksum) = 64", name="ck_mapping_review_checksum"),
        CheckConstraint(
            "decision <> 'approved' OR "
            "(resulting_status = 'approved' AND validation_run_id IS NOT NULL AND "
            "approved_artifact_id IS NOT NULL AND approved_artifact_version IS NOT NULL)",
            name="ck_mapping_review_approval_requirements",
        ),
        UniqueConstraint(
            "mapping_proposal_id", "sequence_number", name="uq_mapping_review_proposal_sequence"
        ),
        UniqueConstraint(
            "mapping_proposal_id", "decision_checksum", name="uq_mapping_review_proposal_checksum"
        ),
        Index("ix_mapping_review_proposal_created", "mapping_proposal_id", "created_at"),
        Index("ix_mapping_review_reviewer_created", "reviewer_user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    mapping_proposal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mapping_proposals.id", ondelete="RESTRICT"), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[ReviewDecisionKind] = mapped_column(
        _string_enum(ReviewDecisionKind, name="mapping_review_decision_kind"), nullable=False
    )
    resulting_status: Mapped[GovernanceWorkflowStatus] = mapped_column(
        _string_enum(GovernanceWorkflowStatus, name="mapping_review_resulting_status"),
        nullable=False,
    )
    reviewer_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_role_snapshot: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False, default=list)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    canonical_resolution: Mapped[str | None] = mapped_column(String(40))
    canonical_entity_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    validation_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mapping_validation_runs.id", ondelete="RESTRICT")
    )
    approved_artifact_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    approved_artifact_version: Mapped[int | None] = mapped_column(Integer)
    decision_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mapping_review_decisions.id", ondelete="RESTRICT")
    )
    policy_schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class KnowledgeEntityRecord(Base):
    """Canonical identity registry; labels/aliases never replace its stable UUID."""

    __tablename__ = "knowledge_entities"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('effect', 'technique', 'method', 'psychology_principle', "
            "'performer', 'source', 'cognitive_mechanism', 'research_paper')",
            name="knowledge_entity_type",
        ),
        CheckConstraint("status IN ('active', 'merged')", name="canonical_entity_status"),
        CheckConstraint("length(trim(canonical_key)) > 0", name="ck_knowledge_entities_key_present"),
        CheckConstraint("length(trim(canonical_name)) > 0", name="ck_knowledge_entities_name_present"),
        CheckConstraint(
            "(status = 'merged' AND merged_into_entity_id IS NOT NULL) OR "
            "(status = 'active' AND merged_into_entity_id IS NULL)",
            name="ck_knowledge_entities_merge_state",
        ),
        CheckConstraint(
            "merged_into_entity_id IS NULL OR merged_into_entity_id <> id",
            name="ck_knowledge_entities_no_self_merge",
        ),
        UniqueConstraint("canonical_key", name="uq_knowledge_entities_canonical_key"),
        Index("ix_knowledge_entities_type_status", "entity_type", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    entity_type: Mapped[EntityType] = mapped_column(
        _string_enum(EntityType, name="knowledge_entity_type"), nullable=False
    )
    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_key: Mapped[str] = mapped_column(String(512), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON_VALUE, nullable=False, default=list)
    description: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    status: Mapped[CanonicalEntityStatus] = mapped_column(
        _string_enum(CanonicalEntityStatus, name="canonical_entity_status"),
        nullable=False,
        default=CanonicalEntityStatus.ACTIVE,
        server_default=CanonicalEntityStatus.ACTIVE.value,
    )
    merged_into_entity_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_entities.id", ondelete="RESTRICT")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=func.now()
    )


class KnowledgeNodeVersionRecord(Base):
    """Immutable persisted KnowledgeNodeVersion payload."""

    __tablename__ = "knowledge_node_versions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="ck_knowledge_node_versions_positive"),
        CheckConstraint("length(payload_checksum) = 64", name="ck_knowledge_node_versions_checksum"),
        UniqueConstraint("entity_id", "version_number", name="uq_knowledge_node_entity_version"),
        UniqueConstraint("mapping_review_decision_id", name="uq_knowledge_node_review_decision"),
        Index("ix_knowledge_node_entity_created", "entity_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    entity_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_entities.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_proposal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mapping_proposals.id", ondelete="RESTRICT"), nullable=False
    )
    mapping_review_decision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mapping_review_decisions.id", ondelete="RESTRICT"), nullable=False
    )
    supersedes_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_node_versions.id", ondelete="RESTRICT")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class KnowledgeRelationshipRecord(Base):
    """Stable directed relationship identity independent of assertion wording."""

    __tablename__ = "knowledge_relationships"
    __table_args__ = (
        CheckConstraint(
            "relation_type IN ('uses', 'inspired_by', 'requires', 'explains', 'performed_by', 'related_to')",
            name="knowledge_relationship_type",
        ),
        CheckConstraint("source_entity_id <> target_entity_id", name="ck_knowledge_relationship_no_self"),
        UniqueConstraint("canonical_key", name="uq_knowledge_relationship_canonical_key"),
        UniqueConstraint(
            "source_entity_id", "relation_type", "target_entity_id",
            name="uq_knowledge_relationship_endpoints_type",
        ),
        Index("ix_knowledge_relationship_source", "source_entity_id", "relation_type"),
        Index("ix_knowledge_relationship_target", "target_entity_id", "relation_type"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    source_entity_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_entities.id", ondelete="RESTRICT"), nullable=False
    )
    target_entity_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_entities.id", ondelete="RESTRICT"), nullable=False
    )
    relation_type: Mapped[RelationType] = mapped_column(
        _string_enum(RelationType, name="knowledge_relationship_type"), nullable=False
    )
    canonical_key: Mapped[str] = mapped_column(String(1200), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class KnowledgeRelationshipAssertionRecord(Base):
    """Immutable persisted KnowledgeRelationshipAssertion and projection context."""

    __tablename__ = "knowledge_relationship_assertions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="ck_relationship_assertions_positive"),
        CheckConstraint("length(payload_checksum) = 64", name="ck_relationship_assertions_checksum"),
        UniqueConstraint("relationship_id", "version_number", name="uq_relationship_assertion_version"),
        UniqueConstraint("mapping_review_decision_id", name="uq_relationship_assertion_review_decision"),
        Index("ix_relationship_assertion_relationship_created", "relationship_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    relationship_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_relationships.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    projection_context: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    payload_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_proposal_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mapping_proposals.id", ondelete="RESTRICT"), nullable=False
    )
    mapping_review_decision_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("mapping_review_decisions.id", ondelete="RESTRICT"), nullable=False
    )
    supersedes_assertion_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("knowledge_relationship_assertions.id", ondelete="RESTRICT")
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class AppendOnlyMutationError(RuntimeError):
    """Raised when application code attempts to rewrite immutable history."""


@event.listens_for(AuditEvent, "before_update", propagate=True)
def _reject_audit_update(_mapper: Any, _connection: Any, _target: AuditEvent) -> None:
    raise AppendOnlyMutationError("audit events are append-only")


@event.listens_for(AuditEvent, "before_delete", propagate=True)
def _reject_audit_delete(_mapper: Any, _connection: Any, _target: AuditEvent) -> None:
    raise AppendOnlyMutationError("audit events are append-only")


def _reject_governance_update(_mapper: Any, _connection: Any, target: Any) -> None:
    raise AppendOnlyMutationError(f"{target.__tablename__} records are immutable")


def _reject_governance_delete(_mapper: Any, _connection: Any, target: Any) -> None:
    raise AppendOnlyMutationError(f"{target.__tablename__} records are immutable")


def _guard_claim_candidate_insert(
    _mapper: Any,
    _connection: Any,
    target: ClaimCandidate,
) -> None:
    status = target.status or GovernanceWorkflowStatus.DRAFT
    if status not in {
        GovernanceWorkflowStatus.DRAFT,
        GovernanceWorkflowStatus.SUBMITTED,
    }:
        raise AppendOnlyMutationError(
            "claim candidate initial status must be draft or submitted"
        )


def _guard_source_permission_request_insert(
    _mapper: Any,
    _connection: Any,
    target: SourcePermissionRequest,
) -> None:
    if not isinstance(target.requested_scope_locators, list) or any(
        not isinstance(value, str) for value in target.requested_scope_locators
    ):
        raise ValueError("requested_scope_locators must be a JSON array of strings")
    if not isinstance(target.actor_role_snapshot, list) or any(
        not isinstance(value, str) for value in target.actor_role_snapshot
    ):
        raise ValueError("actor_role_snapshot must be a JSON array of strings")
    if (
        not isinstance(target.rights_evidence, list)
        or not target.rights_evidence
        or any(
            not isinstance(value, str) or not value.strip()
            for value in target.rights_evidence
        )
    ):
        raise ValueError(
            "rights_evidence must be a non-empty JSON array of non-blank strings"
        )


def _guard_extraction_job_insert(
    _mapper: Any,
    _connection: Any,
    target: ExtractionJob,
) -> None:
    if (target.status or ExtractionJobStatus.QUEUED) != ExtractionJobStatus.QUEUED:
        raise AppendOnlyMutationError("extraction job initial status must be queued")
    if target.attempt_count not in (None, 0):
        raise AppendOnlyMutationError("queued extraction job cannot contain attempts")


def _guard_extraction_job_update(
    _mapper: Any,
    _connection: Any,
    target: ExtractionJob,
) -> None:
    allowed = {
        "status",
        "attempt_count",
        "result_checksum",
        "error_code",
        "started_at",
        "finished_at",
        "updated_at",
        "row_version",
    }
    state = sa_inspect(target)
    changed = {
        attribute.key
        for attribute in state.attrs
        if attribute.key not in allowed and attribute.history.has_changes()
    }
    if changed:
        raise AppendOnlyMutationError(
            "extraction_jobs specification fields are immutable: "
            + ", ".join(sorted(changed))
        )

    history = state.attrs.status.history
    if not history.has_changes():
        lifecycle_changes = {
            field
            for field in (
                "attempt_count",
                "result_checksum",
                "error_code",
                "started_at",
                "finished_at",
            )
            if state.attrs[field].history.has_changes()
        }
        if lifecycle_changes:
            raise AppendOnlyMutationError(
                "extraction job lifecycle fields require a status transition: "
                + ", ".join(sorted(lifecycle_changes))
            )
        return
    previous = history.deleted[0] if history.deleted else None
    current = history.added[0] if history.added else target.status
    transitions = {
        ExtractionJobStatus.QUEUED: {ExtractionJobStatus.RUNNING},
        ExtractionJobStatus.RUNNING: {
            ExtractionJobStatus.SUCCEEDED,
            ExtractionJobStatus.FAILED,
        },
        ExtractionJobStatus.FAILED: {ExtractionJobStatus.RUNNING},
        ExtractionJobStatus.SUCCEEDED: set(),
    }
    if previous not in transitions or current not in transitions[previous]:
        raise AppendOnlyMutationError(
            f"invalid extraction job transition: {previous} -> {current}"
        )
    attempt_history = state.attrs.attempt_count.history
    previous_attempt_count = (
        attempt_history.deleted[0]
        if attempt_history.deleted
        else target.attempt_count
    )
    if previous in {ExtractionJobStatus.QUEUED, ExtractionJobStatus.FAILED}:
        if target.attempt_count != previous_attempt_count + 1:
            raise AppendOnlyMutationError(
                "starting extraction must increment attempt count once"
            )
    elif target.attempt_count != previous_attempt_count:
        raise AppendOnlyMutationError(
            "finishing extraction cannot change attempt count"
        )


def _guard_extraction_proposal_insert(
    _mapper: Any,
    connection: Any,
    target: ExtractionProposalArtifact,
) -> None:
    active = connection.execute(
        select(
            ExtractionJob.status,
            ExtractionJob.attempt_count,
            ExtractionJob.sensitivity,
        ).where(ExtractionJob.id == target.extraction_job_id)
    ).one_or_none()
    if active is None or active != (
        ExtractionJobStatus.RUNNING,
        target.attempt_number,
        target.sensitivity,
    ):
        raise AppendOnlyMutationError(
            "proposal artifact requires the exact active extraction attempt"
        )


def _guard_mapping_proposal_insert(
    _mapper: Any,
    _connection: Any,
    target: MappingProposalRecord,
) -> None:
    if target.status != GovernanceWorkflowStatus.SUBMITTED:
        raise AppendOnlyMutationError(
            "mapping proposal initial status must be submitted"
        )


def _guard_mapping_proposal_update(
    _mapper: Any,
    connection: Any,
    target: MappingProposalRecord,
) -> None:
    allowed = {
        "status",
        "approved_artifact_id",
        "approved_artifact_version",
        "updated_at",
        "row_version",
    }
    state = sa_inspect(target)
    changed = {
        attribute.key
        for attribute in state.attrs
        if attribute.key not in allowed and attribute.history.has_changes()
    }
    if changed:
        raise AppendOnlyMutationError(
            "mapping_proposals protected fields are immutable: "
            + ", ".join(sorted(changed))
        )
    if state.attrs.status.history.has_changes():
        latest = connection.execute(
            select(
                MappingReviewDecision.resulting_status,
                MappingReviewDecision.approved_artifact_id,
                MappingReviewDecision.approved_artifact_version,
            )
            .where(MappingReviewDecision.mapping_proposal_id == target.id)
            .order_by(MappingReviewDecision.sequence_number.desc())
            .limit(1)
        ).one_or_none()
        if latest is None or latest[0] != target.status:
            raise AppendOnlyMutationError(
                "mapping proposal status must match the latest append-only review decision"
            )
        if target.status == GovernanceWorkflowStatus.APPROVED and (
            latest[1] != target.approved_artifact_id
            or latest[2] != target.approved_artifact_version
        ):
            raise AppendOnlyMutationError(
                "mapping proposal artifact must match the latest review decision"
            )


def _guard_knowledge_entity_update(
    _mapper: Any,
    _connection: Any,
    target: KnowledgeEntityRecord,
) -> None:
    allowed = {"aliases", "status", "merged_into_entity_id", "updated_at"}
    state = sa_inspect(target)
    changed = {
        attribute.key
        for attribute in state.attrs
        if attribute.key not in allowed and attribute.history.has_changes()
    }
    if changed:
        raise AppendOnlyMutationError(
            "knowledge_entities identity fields are immutable: "
            + ", ".join(sorted(changed))
        )


def _guard_workflow_envelope_update(
    _mapper: Any,
    connection: Any,
    target: Source | ClaimCandidate,
) -> None:
    allowed = (
        {"updated_at", "row_version"}
        if isinstance(target, Source)
        else {"status", "updated_at", "row_version"}
    )
    state = sa_inspect(target)
    changed = {
        attribute.key
        for attribute in state.attrs
        if attribute.key not in allowed and attribute.history.has_changes()
    }
    if changed:
        fields = ", ".join(sorted(changed))
        raise AppendOnlyMutationError(
            f"{target.__tablename__} protected fields are immutable: {fields}"
        )
    if isinstance(target, ClaimCandidate) and state.attrs.status.history.has_changes():
        latest_resulting_status = connection.execute(
            select(ClaimReviewDecision.resulting_status)
            .where(
                ClaimReviewDecision.claim_candidate_id == target.id,
            )
            .order_by(ClaimReviewDecision.sequence_number.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest_resulting_status != target.status:
            raise AppendOnlyMutationError(
                "claim candidate status must match the latest append-only review decision"
            )


event.listen(
    ClaimCandidate,
    "before_insert",
    _guard_claim_candidate_insert,
    propagate=True,
)
event.listen(
    SourcePermissionRequest,
    "before_insert",
    _guard_source_permission_request_insert,
    propagate=True,
)
event.listen(
    ExtractionJob,
    "before_insert",
    _guard_extraction_job_insert,
    propagate=True,
)
event.listen(
    ExtractionProposalArtifact,
    "before_insert",
    _guard_extraction_proposal_insert,
    propagate=True,
)
event.listen(
    ExtractionJob,
    "before_update",
    _guard_extraction_job_update,
    propagate=True,
)
event.listen(
    ExtractionJob,
    "before_delete",
    _reject_governance_delete,
    propagate=True,
)

event.listen(
    MappingProposalRecord,
    "before_insert",
    _guard_mapping_proposal_insert,
    propagate=True,
)
event.listen(
    MappingProposalRecord,
    "before_update",
    _guard_mapping_proposal_update,
    propagate=True,
)
event.listen(
    MappingProposalRecord,
    "before_delete",
    _reject_governance_delete,
    propagate=True,
)
event.listen(
    KnowledgeEntityRecord,
    "before_update",
    _guard_knowledge_entity_update,
    propagate=True,
)
event.listen(
    KnowledgeEntityRecord,
    "before_delete",
    _reject_governance_delete,
    propagate=True,
)


for _workflow_envelope in (Source, ClaimCandidate):
    event.listen(
        _workflow_envelope,
        "before_update",
        _guard_workflow_envelope_update,
        propagate=True,
    )
    event.listen(
        _workflow_envelope,
        "before_delete",
        _reject_governance_delete,
        propagate=True,
    )


for _immutable_model in (
    SourceVersion,
    CitationVerificationEvidence,
    SourcePermissionRequest,
    SourceReviewDecision,
    ExtractionAttempt,
    ExtractionProposalArtifact,
    ClaimReviewDecision,
    EvidenceCardVersion,
    MappingProposalEvidence,
    MappingValidationRun,
    MappingReviewDecision,
    KnowledgeNodeVersionRecord,
    KnowledgeRelationshipRecord,
    KnowledgeRelationshipAssertionRecord,
):
    event.listen(
        _immutable_model,
        "before_update",
        _reject_governance_update,
        propagate=True,
    )
    event.listen(
        _immutable_model,
        "before_delete",
        _reject_governance_delete,
        propagate=True,
    )


__all__ = [
    "AppendOnlyMutationError",
    "AuditEvent",
    "Base",
    "CandidateCreatedByKind",
    "CanonicalEntityStatus",
    "CitationVerificationEvidence",
    "ClaimCandidate",
    "ClaimReviewDecision",
    "EvidenceCardVersion",
    "ExtractionAttempt",
    "ExtractionAttemptStatus",
    "ExtractionJob",
    "ExtractionJobStatus",
    "ExtractionProposalArtifact",
    "GovernanceWorkflowStatus",
    "IdempotencyRecord",
    "IdempotencyStatus",
    "KnowledgeEntityRecord",
    "KnowledgeNodeVersionRecord",
    "KnowledgeRelationshipAssertionRecord",
    "KnowledgeRelationshipRecord",
    "MappingProposalEvidence",
    "MappingProposalRecord",
    "MappingReviewDecision",
    "MappingValidationPhase",
    "MappingValidationRun",
    "JSON_VALUE",
    "ReviewDecisionKind",
    "Role",
    "RoleName",
    "SessionRecord",
    "SessionStatus",
    "SessionTransport",
    "Source",
    "SourcePermissionRequest",
    "SourceReviewDecision",
    "SourceVersion",
    "User",
    "UserRole",
    "UserStatus",
    "utc_now",
]
