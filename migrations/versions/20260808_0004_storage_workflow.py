"""Add governed storage, ingestion, and corpus activation persistence.

Revision ID: 20260808_0004
Revises: 20260808_0003
Create Date: 2026-08-08
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260808_0004"
down_revision: str | None = "20260808_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        create_constraint=False,
    )


STORAGE_MANIFEST_STATE = _enum(
    "storage_manifest_state",
    "pending",
    "authorized",
    "ingested",
)
INGESTION_OPERATION_STATE = _enum(
    "ingestion_operation_state",
    "running",
    "succeeded",
    "failed_cleaned",
    "reconciliation_required",
)
CORPUS_ACTIVATION_STATE = _enum(
    "corpus_activation_state",
    "staged",
    "active",
    "inactive",
)


IMMUTABLE_TABLES = (
    "storage_manifest_items",
    "manifest_authorizations",
    "ingestion_receipts",
)


def upgrade() -> None:
    op.create_table(
        "storage_manifests",
        sa.Column("id", UUID, nullable=False),
        sa.Column("corpus_id", UUID, nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column(
            "projection_schema_version", sa.String(length=80), nullable=False
        ),
        sa.Column("collection_name", sa.String(length=255), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("manifest_payload", JSONB, nullable=False),
        sa.Column("validation_rule_versions", JSONB, nullable=False),
        sa.Column("authorized_sensitive_levels", JSONB, nullable=False),
        sa.Column("expected_point_count", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            STORAGE_MANIFEST_STATE,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("created_by_user_id", UUID, nullable=False),
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
            "status IN ('pending', 'authorized', 'ingested')",
            name="storage_manifest_state",
        ),
        sa.CheckConstraint(
            "length(manifest_hash) = 64", name="ck_storage_manifest_hash"
        ),
        sa.CheckConstraint(
            "expected_point_count > 0", name="ck_storage_manifest_points"
        ),
        sa.CheckConstraint(
            "row_version > 0", name="ck_storage_manifest_row_version"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_storage_manifests"),
        sa.UniqueConstraint("manifest_hash", name="uq_storage_manifest_hash"),
    )
    op.create_index(
        "ix_storage_manifest_corpus_status",
        "storage_manifests",
        ["corpus_id", "status"],
        unique=False,
    )

    op.create_table(
        "storage_manifest_items",
        sa.Column("id", UUID, nullable=False),
        sa.Column("manifest_id", UUID, nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("artifact_type", sa.String(length=80), nullable=False),
        sa.Column("artifact_row_id", UUID, nullable=False),
        sa.Column("artifact_domain_id", UUID, nullable=False),
        sa.Column("artifact_version", sa.Integer(), nullable=False),
        sa.Column("payload_checksum", sa.String(length=64), nullable=False),
        sa.Column("projection_checksum", sa.String(length=64), nullable=False),
        sa.Column("projection_point_id", UUID, nullable=False),
        sa.Column("sensitivity", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "sequence_number > 0", name="ck_manifest_item_sequence"
        ),
        sa.CheckConstraint(
            "artifact_version > 0", name="ck_manifest_item_version"
        ),
        sa.CheckConstraint(
            "length(payload_checksum) = 64", name="ck_manifest_item_checksum"
        ),
        sa.CheckConstraint(
            "length(projection_checksum) = 64",
            name="ck_manifest_item_projection_checksum",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"], ["storage_manifests.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_storage_manifest_items"),
        sa.UniqueConstraint(
            "manifest_id", "sequence_number", name="uq_manifest_item_sequence"
        ),
        sa.UniqueConstraint(
            "manifest_id",
            "artifact_type",
            "artifact_row_id",
            name="uq_manifest_item_artifact",
        ),
    )

    op.create_table(
        "manifest_authorizations",
        sa.Column("id", UUID, nullable=False),
        sa.Column("manifest_id", UUID, nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("authorizer_user_id", UUID, nullable=False),
        sa.Column("actor_role_snapshot", JSONB, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("validation_snapshot", JSONB, nullable=False),
        sa.Column(
            "authorized_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(manifest_hash) = 64",
            name="ck_manifest_authorization_hash",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_manifest_authorization_reason",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"], ["storage_manifests.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["authorizer_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_manifest_authorizations"),
        sa.UniqueConstraint(
            "manifest_id", name="uq_manifest_authorization_once"
        ),
    )

    op.create_table(
        "ingestion_operations",
        sa.Column("id", UUID, nullable=False),
        sa.Column("manifest_id", UUID, nullable=False),
        sa.Column("actor_user_id", UUID, nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("state", INGESTION_OPERATION_STATE, nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("cleanup_outcome", sa.String(length=80), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('running', 'succeeded', 'failed_cleaned', "
            "'reconciliation_required')",
            name="ingestion_operation_state",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64", name="ck_ingestion_request_hash"
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"], ["storage_manifests.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_operations"),
        sa.UniqueConstraint(
            "actor_user_id",
            "idempotency_key",
            name="uq_ingestion_actor_idempotency",
        ),
    )
    op.create_index(
        "ix_ingestion_manifest_state",
        "ingestion_operations",
        ["manifest_id", "state"],
        unique=False,
    )
    op.create_index(
        "uq_ingestion_one_active_per_manifest",
        "ingestion_operations",
        ["manifest_id"],
        unique=True,
        postgresql_where=sa.text(
            "state IN ('running', 'reconciliation_required')"
        ),
    )

    op.create_table(
        "ingestion_receipts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("manifest_id", UUID, nullable=False),
        sa.Column("corpus_id", UUID, nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("collection_name", sa.String(length=255), nullable=False),
        sa.Column("receipt_payload", JSONB, nullable=False),
        sa.Column("payload_checksum", sa.String(length=64), nullable=False),
        sa.Column("operation_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(manifest_hash) = 64", name="ck_ingestion_receipt_hash"
        ),
        sa.CheckConstraint(
            "length(payload_checksum) = 64",
            name="ck_ingestion_receipt_checksum",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"], ["storage_manifests.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["ingestion_operations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_receipts"),
        sa.UniqueConstraint("manifest_id", name="uq_ingestion_receipt_manifest"),
    )

    op.create_table(
        "corpus_versions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("manifest_id", UUID, nullable=False),
        sa.Column("ingestion_receipt_id", UUID, nullable=False),
        sa.Column("runtime_scope", sa.String(length=120), nullable=False),
        sa.Column("qdrant_collection", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("projection_version", sa.String(length=80), nullable=False),
        sa.Column("vector_size", sa.Integer(), nullable=False),
        sa.Column("vector_distance", sa.String(length=40), nullable=False),
        sa.Column(
            "activation_state",
            CORPUS_ACTIVATION_STATE,
            server_default="staged",
            nullable=False,
        ),
        sa.Column("created_by_user_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "activation_state IN ('staged', 'active', 'inactive')",
            name="corpus_activation_state",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id"], ["storage_manifests.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_receipt_id"],
            ["ingestion_receipts.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_corpus_versions"),
        sa.UniqueConstraint("manifest_id", name="uq_corpus_manifest"),
    )
    op.create_index(
        "ix_corpus_scope_state",
        "corpus_versions",
        ["runtime_scope", "activation_state"],
        unique=False,
    )
    op.create_index(
        "uq_corpus_one_active_per_scope",
        "corpus_versions",
        ["runtime_scope"],
        unique=True,
        postgresql_where=sa.text("activation_state = 'active'"),
    )

    op.create_table(
        "active_corpus_pointers",
        sa.Column("runtime_scope", sa.String(length=120), nullable=False),
        sa.Column("corpus_id", UUID, nullable=False),
        sa.Column("activated_by_user_id", UUID, nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint(
            "row_version > 0", name="ck_active_corpus_row_version"
        ),
        sa.ForeignKeyConstraint(
            ["corpus_id"], ["corpus_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["activated_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint(
            "runtime_scope", name="pk_active_corpus_pointers"
        ),
    )

    for table_name in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW
            EXECUTE FUNCTION magicforge_reject_governance_record_mutation()
            """
        )

    op.execute(
        """
        CREATE FUNCTION magicforge_guard_storage_manifest_workflow()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $magicforge$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'pending' THEN
                    RAISE EXCEPTION
                        'storage manifest initial status must be pending'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'storage manifest records cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - 'status' - 'manifest_payload' - 'updated_at' -
                    'row_version') IS DISTINCT FROM
               (to_jsonb(OLD) - 'status' - 'manifest_payload' - 'updated_at' -
                    'row_version') THEN
                RAISE EXCEPTION 'storage manifest identity fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status = 'pending' AND NEW.status = 'authorized') OR
                (OLD.status = 'authorized' AND NEW.status = 'ingested')
            ) THEN
                RAISE EXCEPTION 'invalid storage manifest status transition'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.manifest_payload IS DISTINCT FROM OLD.manifest_payload AND
               NEW.status IS NOT DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION
                    'storage manifest payload may change only with status'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $magicforge$
        """
    )
    op.execute(
        """
        CREATE TRIGGER storage_manifests_workflow_guard
        BEFORE INSERT OR UPDATE OR DELETE ON storage_manifests
        FOR EACH ROW
        EXECUTE FUNCTION magicforge_guard_storage_manifest_workflow()
        """
    )

    op.execute(
        """
        CREATE FUNCTION magicforge_guard_ingestion_operation_workflow()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $magicforge$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'running' THEN
                    RAISE EXCEPTION
                        'ingestion operation initial state must be running'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'ingestion operation records cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - 'state' - 'attempt_count' - 'cleanup_outcome' -
                    'error_code' - 'finished_at') IS DISTINCT FROM
               (to_jsonb(OLD) - 'state' - 'attempt_count' - 'cleanup_outcome' -
                    'error_code' - 'finished_at') THEN
                RAISE EXCEPTION 'ingestion operation identity fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF OLD.state <> 'running' THEN
                RAISE EXCEPTION 'terminal ingestion operations are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.state NOT IN (
                'succeeded', 'failed_cleaned', 'reconciliation_required'
            ) THEN
                RAISE EXCEPTION 'invalid ingestion operation state transition'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.finished_at IS NULL THEN
                RAISE EXCEPTION
                    'terminal ingestion operation requires finished_at'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $magicforge$
        """
    )
    op.execute(
        """
        CREATE TRIGGER ingestion_operations_workflow_guard
        BEFORE INSERT OR UPDATE OR DELETE ON ingestion_operations
        FOR EACH ROW
        EXECUTE FUNCTION magicforge_guard_ingestion_operation_workflow()
        """
    )

    op.execute(
        """
        CREATE FUNCTION magicforge_guard_corpus_version_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $magicforge$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.activation_state <> 'staged' OR
                   NEW.activated_at IS NOT NULL THEN
                    RAISE EXCEPTION
                        'corpus version initial state must be staged'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'corpus version records cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - 'runtime_scope' - 'activation_state' -
                    'activated_at')
                    IS DISTINCT FROM
               (to_jsonb(OLD) - 'runtime_scope' - 'activation_state' -
                    'activated_at') THEN
                RAISE EXCEPTION 'corpus version identity fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.runtime_scope IS DISTINCT FROM OLD.runtime_scope AND NOT (
                OLD.activation_state = 'staged' AND
                NEW.activation_state = 'active'
            ) THEN
                RAISE EXCEPTION 'corpus runtime scope becomes fixed at activation'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.activation_state IS DISTINCT FROM OLD.activation_state AND
               NOT (
                   (OLD.activation_state IN ('staged', 'inactive') AND
                    NEW.activation_state = 'active') OR
                   (OLD.activation_state = 'active' AND
                    NEW.activation_state = 'inactive')
               ) THEN
                RAISE EXCEPTION 'invalid corpus activation state transition'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.activation_state = 'active' AND NEW.activated_at IS NULL THEN
                RAISE EXCEPTION 'active corpus requires activated_at'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $magicforge$
        """
    )
    op.execute(
        """
        CREATE TRIGGER corpus_versions_identity_guard
        BEFORE INSERT OR UPDATE OR DELETE ON corpus_versions
        FOR EACH ROW
        EXECUTE FUNCTION magicforge_guard_corpus_version_identity()
        """
    )

    op.execute(
        """
        CREATE FUNCTION magicforge_guard_active_corpus_pointer_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $magicforge$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'active corpus pointers cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.runtime_scope IS DISTINCT FROM OLD.runtime_scope THEN
                RAISE EXCEPTION 'active corpus pointer scope is immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.row_version <= OLD.row_version THEN
                RAISE EXCEPTION 'active corpus pointer version must increase'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $magicforge$
        """
    )
    op.execute(
        """
        CREATE TRIGGER active_corpus_pointers_identity_guard
        BEFORE UPDATE OR DELETE ON active_corpus_pointers
        FOR EACH ROW
        EXECUTE FUNCTION magicforge_guard_active_corpus_pointer_identity()
        """
    )

    # These constraint triggers are deferred so the service can persist the
    # append-only child and advance its workflow envelope in either flush
    # order while still making an inconsistent transaction impossible to
    # commit.
    op.execute(
        """
        CREATE FUNCTION magicforge_validate_storage_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $magicforge$
        DECLARE
            manifest_record storage_manifests%ROWTYPE;
            operation_record ingestion_operations%ROWTYPE;
            receipt_record ingestion_receipts%ROWTYPE;
            corpus_record corpus_versions%ROWTYPE;
        BEGIN
            IF TG_TABLE_NAME = 'manifest_authorizations' THEN
                SELECT * INTO manifest_record
                  FROM storage_manifests
                 WHERE id = NEW.manifest_id;
                IF NOT FOUND OR
                   manifest_record.manifest_hash <> NEW.manifest_hash OR
                   manifest_record.status NOT IN ('authorized', 'ingested') THEN
                    RAISE EXCEPTION
                        'manifest authorization identity is inconsistent'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'ingestion_receipts' THEN
                SELECT * INTO manifest_record
                  FROM storage_manifests
                 WHERE id = NEW.manifest_id;
                SELECT * INTO operation_record
                  FROM ingestion_operations
                 WHERE id = NEW.operation_id;
                IF manifest_record.id IS NULL OR operation_record.id IS NULL OR
                   manifest_record.corpus_id <> NEW.corpus_id OR
                   manifest_record.manifest_hash <> NEW.manifest_hash OR
                   manifest_record.collection_name <> NEW.collection_name OR
                   manifest_record.status <> 'ingested' OR
                   operation_record.manifest_id <> NEW.manifest_id OR
                   operation_record.state <> 'succeeded' THEN
                    RAISE EXCEPTION
                        'ingestion receipt identity is inconsistent'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'corpus_versions' THEN
                SELECT * INTO manifest_record
                  FROM storage_manifests
                 WHERE id = NEW.manifest_id;
                SELECT * INTO receipt_record
                  FROM ingestion_receipts
                 WHERE id = NEW.ingestion_receipt_id;
                IF manifest_record.id IS NULL OR receipt_record.id IS NULL OR
                   manifest_record.corpus_id <> NEW.id OR
                   manifest_record.status <> 'ingested' OR
                   receipt_record.manifest_id <> NEW.manifest_id OR
                   receipt_record.corpus_id <> NEW.id OR
                   receipt_record.collection_name <> NEW.qdrant_collection THEN
                    RAISE EXCEPTION
                        'corpus version identity is inconsistent'
                        USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'active_corpus_pointers' THEN
                SELECT * INTO corpus_record
                  FROM corpus_versions
                 WHERE id = NEW.corpus_id;
                IF corpus_record.id IS NULL OR
                   corpus_record.activation_state <> 'active' OR
                   corpus_record.runtime_scope <> NEW.runtime_scope THEN
                    RAISE EXCEPTION
                        'active corpus pointer target is inconsistent'
                        USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $magicforge$
        """
    )
    for table_name in (
        "manifest_authorizations",
        "ingestion_receipts",
        "corpus_versions",
        "active_corpus_pointers",
    ):
        op.execute(
            f"""
            CREATE CONSTRAINT TRIGGER {table_name}_identity_consistency
            AFTER INSERT OR UPDATE ON {table_name}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION magicforge_validate_storage_identity()
            """
        )


def downgrade() -> None:
    for table_name in reversed(
        (
            "manifest_authorizations",
            "ingestion_receipts",
            "corpus_versions",
            "active_corpus_pointers",
        )
    ):
        op.execute(
            f"DROP TRIGGER {table_name}_identity_consistency ON {table_name}"
        )
    op.execute("DROP FUNCTION magicforge_validate_storage_identity()")

    op.execute(
        "DROP TRIGGER active_corpus_pointers_identity_guard "
        "ON active_corpus_pointers"
    )
    op.execute("DROP FUNCTION magicforge_guard_active_corpus_pointer_identity()")
    op.execute("DROP TRIGGER corpus_versions_identity_guard ON corpus_versions")
    op.execute("DROP FUNCTION magicforge_guard_corpus_version_identity()")
    op.execute(
        "DROP TRIGGER ingestion_operations_workflow_guard "
        "ON ingestion_operations"
    )
    op.execute("DROP FUNCTION magicforge_guard_ingestion_operation_workflow()")
    op.execute(
        "DROP TRIGGER storage_manifests_workflow_guard ON storage_manifests"
    )
    op.execute("DROP FUNCTION magicforge_guard_storage_manifest_workflow()")

    for table_name in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER {table_name}_immutable ON {table_name}")

    op.drop_table("active_corpus_pointers")

    op.drop_index(
        "uq_corpus_one_active_per_scope", table_name="corpus_versions"
    )
    op.drop_index("ix_corpus_scope_state", table_name="corpus_versions")
    op.drop_table("corpus_versions")

    op.drop_table("ingestion_receipts")

    op.drop_index(
        "uq_ingestion_one_active_per_manifest",
        table_name="ingestion_operations",
    )
    op.drop_index(
        "ix_ingestion_manifest_state", table_name="ingestion_operations"
    )
    op.drop_table("ingestion_operations")

    op.drop_table("manifest_authorizations")
    op.drop_table("storage_manifest_items")

    op.drop_index(
        "ix_storage_manifest_corpus_status", table_name="storage_manifests"
    )
    op.drop_table("storage_manifests")
