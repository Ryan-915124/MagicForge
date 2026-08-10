"""Create the transactional governance foundation.

Revision ID: 20260807_0001
Revises:
Create Date: 2026-08-07
"""

from collections.abc import Sequence
from uuid import UUID as PythonUUID

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260807_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
USER_STATUS = sa.Enum(
    "active", "disabled", name="user_status", native_enum=False, create_constraint=False
)
ROLE_NAME = sa.Enum(
    "reader",
    "reviewer",
    "operator",
    "admin",
    name="role_name",
    native_enum=False,
    create_constraint=False,
)
SESSION_TRANSPORT = sa.Enum(
    "cookie",
    "bearer",
    name="session_transport",
    native_enum=False,
    create_constraint=False,
)
SESSION_STATUS = sa.Enum(
    "active",
    "revoked",
    "expired",
    name="session_status",
    native_enum=False,
    create_constraint=False,
)
IDEMPOTENCY_STATUS = sa.Enum(
    "in_progress",
    "completed",
    "failed",
    name="idempotency_status",
    native_enum=False,
    create_constraint=False,
)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID, nullable=False),
        sa.Column("username", sa.String(length=80), nullable=False),
        sa.Column("username_normalized", sa.String(length=80), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("email_normalized", sa.String(length=320), nullable=True),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("status", USER_STATUS, server_default="active", nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name="user_status"
        ),
        sa.CheckConstraint("row_version > 0", name="ck_users_row_version_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email_normalized", name="uq_users_email_normalized"),
        sa.UniqueConstraint(
            "username_normalized", name="uq_users_username_normalized"
        ),
    )
    op.create_index(
        "ix_users_status_created_at", "users", ["status", "created_at"], unique=False
    )

    op.create_table(
        "roles",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", ROLE_NAME, nullable=False),
        sa.Column(
            "description", sa.String(length=500), server_default="", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "name IN ('reader', 'reviewer', 'operator', 'admin')",
            name="role_name",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )

    roles = sa.table(
        "roles",
        sa.column("id", UUID),
        sa.column("name", ROLE_NAME),
        sa.column("description", sa.String(length=500)),
    )
    op.bulk_insert(
        roles,
        [
            {
                "id": PythonUUID("284f9d33-7d47-5d81-829d-68ed9f925765"),
                "name": "reader",
                "description": "Read authorized Production knowledge.",
            },
            {
                "id": PythonUUID("0a8671f6-b41f-5c44-bda4-5f2be8c805f3"),
                "name": "reviewer",
                "description": "Review governed sources, claims, and mappings.",
            },
            {
                "id": PythonUUID("b83b6fee-eba4-5db5-a70a-dc06a3ee00b4"),
                "name": "operator",
                "description": "Build, authorize, ingest, and activate corpora.",
            },
            {
                "id": PythonUUID("35112fa5-7e29-53fc-aa8e-099e72b95130"),
                "name": "admin",
                "description": "Administer users, roles, sessions, and audit access.",
            },
        ],
    )

    op.create_table(
        "user_roles",
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("role_id", UUID, nullable=False),
        sa.Column("granted_by_user_id", UUID, nullable=True),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", UUID, nullable=True),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "row_version > 0", name="ck_user_roles_row_version_positive"
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= granted_at",
            name="ck_user_roles_revocation_order",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("user_id", "role_id", name="pk_user_roles"),
    )
    op.create_index(
        "ix_user_roles_role_revoked",
        "user_roles",
        ["role_id", "revoked_at"],
        unique=False,
    )

    op.create_table(
        "sessions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token_hash", sa.String(length=64), nullable=True),
        sa.Column("transport", SESSION_TRANSPORT, nullable=False),
        sa.Column("status", SESSION_STATUS, server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", UUID, nullable=True),
        sa.Column(
            "revocation_reason", sa.String(length=500), server_default="", nullable=False
        ),
        sa.Column("client_metadata", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "transport IN ('cookie', 'bearer')", name="session_transport"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'expired')", name="session_status"
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64", name="ck_sessions_token_hash"
        ),
        sa.CheckConstraint(
            "csrf_token_hash IS NULL OR length(csrf_token_hash) = 64",
            name="ck_sessions_csrf_token_hash",
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_sessions_expiry_order"
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_sessions_revocation_order",
        ),
        sa.CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status <> 'revoked' AND revoked_at IS NULL)",
            name="ck_sessions_status_revocation",
        ),
        sa.CheckConstraint(
            "row_version > 0", name="ck_sessions_row_version_positive"
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    op.create_index(
        "ix_sessions_status_expires",
        "sessions",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_sessions_user_expires",
        "sessions",
        ["user_id", "expires_at"],
        unique=False,
    )

    op.create_table(
        "audit_events",
        sa.Column("event_id", UUID, nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("actor_user_id", UUID, nullable=False),
        sa.Column("actor_role_snapshot", JSONB, nullable=False),
        sa.Column("object_type", sa.String(length=120), nullable=False),
        sa.Column("object_id", sa.String(length=255), nullable=False),
        sa.Column(
            "domain_schema_version",
            sa.String(length=80),
            server_default="audit-event-0.1",
            nullable=False,
        ),
        sa.Column("payload_checksum", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=255), nullable=True),
        sa.Column("correlation_id", sa.String(length=255), nullable=False),
        sa.Column("previous_state", JSONB, nullable=True),
        sa.Column("new_state", JSONB, nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("metadata", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name="ck_audit_events_reason_present"
        ),
        sa.CheckConstraint(
            "length(trim(correlation_id)) > 0",
            name="ck_audit_events_correlation_present",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_audit_events"),
    )
    op.create_index(
        "ix_audit_events_actor_created",
        "audit_events",
        ["actor_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_object_created",
        "audit_events",
        ["object_type", "object_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_type_created",
        "audit_events",
        ["event_type", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_events_request_id", "audit_events", ["request_id"], unique=False
    )
    op.create_index(
        "ix_audit_events_correlation_id",
        "audit_events",
        ["correlation_id"],
        unique=False,
    )

    op.execute(
        """
        CREATE FUNCTION magicforge_reject_audit_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $magicforge$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only'
                USING ERRCODE = '55000';
        END;
        $magicforge$
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW
        EXECUTE FUNCTION magicforge_reject_audit_event_mutation()
        """
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", UUID, nullable=False),
        sa.Column("actor_user_id", UUID, nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status", IDEMPOTENCY_STATUS, server_default="in_progress", nullable=False
        ),
        sa.Column("response_status_code", sa.Integer(), nullable=True),
        sa.Column("response_body", JSONB, nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed')",
            name="idempotency_status",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64", name="ck_idempotency_request_hash"
        ),
        sa.CheckConstraint(
            "response_status_code IS NULL OR "
            "(response_status_code >= 100 AND response_status_code <= 599)",
            name="ck_idempotency_response_status",
        ),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name="ck_idempotency_expiry_order",
        ),
        sa.CheckConstraint(
            "row_version > 0", name="ck_idempotency_row_version_positive"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_records"),
        sa.UniqueConstraint(
            "actor_user_id",
            "scope",
            "idempotency_key",
            name="uq_idempotency_actor_scope_key",
        ),
    )
    op.create_index(
        "ix_idempotency_actor_scope",
        "idempotency_records",
        ["actor_user_id", "scope"],
        unique=False,
    )
    op.create_index(
        "ix_idempotency_status_expires",
        "idempotency_records",
        ["status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_idempotency_status_expires", table_name="idempotency_records"
    )
    op.drop_index(
        "ix_idempotency_actor_scope", table_name="idempotency_records"
    )
    op.drop_table("idempotency_records")

    op.execute("DROP TRIGGER audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION magicforge_reject_audit_event_mutation()")
    op.drop_index("ix_audit_events_correlation_id", table_name="audit_events")
    op.drop_index("ix_audit_events_request_id", table_name="audit_events")
    op.drop_index("ix_audit_events_type_created", table_name="audit_events")
    op.drop_index("ix_audit_events_object_created", table_name="audit_events")
    op.drop_index("ix_audit_events_actor_created", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_index("ix_sessions_user_expires", table_name="sessions")
    op.drop_index("ix_sessions_status_expires", table_name="sessions")
    op.drop_table("sessions")

    op.drop_index("ix_user_roles_role_revoked", table_name="user_roles")
    op.drop_table("user_roles")
    op.drop_table("roles")

    op.drop_index("ix_users_status_created_at", table_name="users")
    op.drop_table("users")
