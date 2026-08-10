"""Add governed Production extraction job and attempt persistence.

Revision ID: 20260809_0007
Revises: 20260808_0006
Create Date: 2026-08-09

The job table binds an immutable extraction specification to one exact,
approved Source chain. Attempt outcomes are append-only and retain only safe
machine error codes and checksums, never raw provider exceptions or output.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "20260809_0007"
down_revision: str | None = "20260808_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_source_versions_id_content_hash",
        "source_versions",
        ["id", "content_hash"],
    )
    op.create_unique_constraint(
        "uq_source_review_id_version_permission",
        "source_review_decisions",
        ["id", "source_version_id", "source_permission_request_id"],
    )

    op.create_table(
        "extraction_jobs",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_review_decision_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_permission_request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("scope_snapshot", JSONB, nullable=False),
        sa.Column("sensitivity", sa.String(length=13), nullable=False),
        sa.Column("llm_provider", sa.String(length=16), server_default="GLM", nullable=False),
        sa.Column("model_name", sa.String(length=255), nullable=False),
        sa.Column("model_snapshot", JSONB, nullable=False),
        sa.Column("prompt_version", sa.String(length=120), nullable=False),
        sa.Column("prompt_snapshot", JSONB, nullable=False),
        sa.Column("output_schema_version", sa.String(length=120), nullable=False),
        sa.Column("schema_snapshot", JSONB, nullable=False),
        sa.Column("chunk_config_snapshot", JSONB, nullable=False),
        sa.Column("configuration_checksum", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=9), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("result_checksum", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("requested_by_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="extraction_job_status",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('public', 'controlled', 'secret_method', 'restricted')",
            name="extraction_job_sensitivity",
        ),
        sa.CheckConstraint("llm_provider = 'GLM'", name="ck_extraction_jobs_glm_only"),
        sa.CheckConstraint("length(content_hash) = 64", name="ck_extraction_jobs_content_hash"),
        sa.CheckConstraint(
            "length(configuration_checksum) = 64",
            name="ck_extraction_jobs_configuration_checksum",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) = 64", name="ck_extraction_jobs_idempotency_key"
        ),
        sa.CheckConstraint(
            "result_checksum IS NULL OR length(result_checksum) = 64",
            name="ck_extraction_jobs_result_checksum",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR (length(trim(error_code)) > 0 AND length(error_code) <= 120)",
            name="ck_extraction_jobs_error_code",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,119}$'",
            name="ck_extraction_jobs_error_code_format",
        ),
        sa.CheckConstraint(
            "length(trim(model_name)) > 0", name="ck_extraction_jobs_model_name_present"
        ),
        sa.CheckConstraint(
            "length(trim(prompt_version)) > 0",
            name="ck_extraction_jobs_prompt_version_present",
        ),
        sa.CheckConstraint(
            "length(trim(output_schema_version)) > 0",
            name="ck_extraction_jobs_schema_version_present",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_extraction_jobs_attempt_count_nonnegative"
        ),
        sa.CheckConstraint(
            "row_version > 0", name="ck_extraction_jobs_row_version_positive"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(scope_snapshot) = 'object'",
            name="ck_extraction_jobs_scope_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(model_snapshot) = 'object'",
            name="ck_extraction_jobs_model_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(prompt_snapshot) = 'object'",
            name="ck_extraction_jobs_prompt_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(schema_snapshot) = 'object'",
            name="ck_extraction_jobs_schema_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(chunk_config_snapshot) = 'object'",
            name="ck_extraction_jobs_chunk_config_object",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_extraction_jobs_time_order",
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id", "content_hash"],
            ["source_versions.id", "source_versions.content_hash"],
            name="fk_extraction_job_source_version_hash",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_extraction_jobs"),
        sa.UniqueConstraint("idempotency_key", name="uq_extraction_jobs_idempotency_key"),
        sa.UniqueConstraint(
            "id", "source_version_id", name="uq_extraction_jobs_id_source_version"
        ),
    )
    op.create_index(
        "ix_extraction_jobs_source_status",
        "extraction_jobs",
        ["source_version_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_extraction_jobs_review_created",
        "extraction_jobs",
        ["source_review_decision_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "extraction_attempts",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_job_id", UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("result_checksum", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("attempt_checksum", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')", name="extraction_attempt_status"
        ),
        sa.CheckConstraint(
            "attempt_number > 0", name="ck_extraction_attempts_number_positive"
        ),
        sa.CheckConstraint(
            "result_checksum IS NULL OR length(result_checksum) = 64",
            name="ck_extraction_attempts_result_checksum",
        ),
        sa.CheckConstraint(
            "length(attempt_checksum) = 64", name="ck_extraction_attempts_checksum"
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR (length(trim(error_code)) > 0 AND length(error_code) <= 120)",
            name="ck_extraction_attempts_error_code",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,119}$'",
            name="ck_extraction_attempts_error_code_format",
        ),
        sa.CheckConstraint(
            "finished_at >= started_at", name="ck_extraction_attempts_time_order"
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND result_checksum IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND result_checksum IS NULL AND error_code IS NOT NULL)",
            name="ck_extraction_attempts_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_job_id"], ["extraction_jobs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_extraction_attempts"),
        sa.UniqueConstraint(
            "extraction_job_id", "attempt_number", name="uq_extraction_attempts_job_number"
        ),
        sa.UniqueConstraint(
            "extraction_job_id", "attempt_checksum", name="uq_extraction_attempts_job_checksum"
        ),
    )
    op.create_index(
        "ix_extraction_attempts_job_finished",
        "extraction_attempts",
        ["extraction_job_id", "finished_at"],
        unique=False,
    )

    op.create_table(
        "extraction_proposal_artifacts",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_job_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("proposal_id", UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_run_id", sa.String(length=255), nullable=False),
        sa.Column("schema_version", sa.String(length=120), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("payload_checksum", sa.String(length=64), nullable=False),
        sa.Column("sensitivity", sa.String(length=13), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(extraction_run_id)) > 0",
            name="ck_extraction_proposals_run_id_present",
        ),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_extraction_proposals_attempt_positive",
        ),
        sa.CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_extraction_proposals_schema_present",
        ),
        sa.CheckConstraint(
            "length(payload_checksum) = 64",
            name="ck_extraction_proposals_checksum",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('public', 'controlled', 'secret_method', 'restricted')",
            name="extraction_proposal_sensitivity",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_extraction_proposals_payload_object",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_job_id", "source_version_id"],
            ["extraction_jobs.id", "extraction_jobs.source_version_id"],
            name="fk_extraction_proposal_job_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_extraction_proposal_artifacts"),
        sa.UniqueConstraint(
            "extraction_job_id", name="uq_extraction_proposals_job"
        ),
        sa.UniqueConstraint("proposal_id", name="uq_extraction_proposals_proposal_id"),
    )
    op.create_index(
        "ix_extraction_proposals_source_created",
        "extraction_proposal_artifacts",
        ["source_version_id", "created_at"],
        unique=False,
    )

    op.execute(
        """
        CREATE TRIGGER extraction_attempts_immutable
        BEFORE UPDATE OR DELETE ON extraction_attempts
        FOR EACH ROW
        EXECUTE FUNCTION magicforge_reject_governance_record_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER extraction_proposal_artifacts_immutable
        BEFORE UPDATE OR DELETE ON extraction_proposal_artifacts
        FOR EACH ROW
        EXECUTE FUNCTION magicforge_reject_governance_record_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION magicforge_guard_extraction_proposal_insert()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $magicforge$
        DECLARE
            job_status text;
            job_attempt_count integer;
            job_sensitivity text;
        BEGIN
            SELECT status, attempt_count, sensitivity
              INTO job_status, job_attempt_count, job_sensitivity
              FROM extraction_jobs
             WHERE id = NEW.extraction_job_id
             FOR UPDATE;
            IF job_status IS DISTINCT FROM 'running' OR
               job_attempt_count IS DISTINCT FROM NEW.attempt_number OR
               job_sensitivity IS DISTINCT FROM NEW.sensitivity THEN
                RAISE EXCEPTION
                    'proposal artifact requires the exact active extraction attempt'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $magicforge$
        """
    )
    op.execute(
        """
        CREATE TRIGGER extraction_proposal_artifacts_insert_guard
        BEFORE INSERT ON extraction_proposal_artifacts
        FOR EACH ROW
        EXECUTE FUNCTION magicforge_guard_extraction_proposal_insert()
        """
    )
    op.execute(
        """
        CREATE FUNCTION magicforge_guard_extraction_job()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $magicforge$
        DECLARE
            old_status text;
            new_status text;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'queued' OR NEW.attempt_count <> 0 THEN
                    RAISE EXCEPTION 'extraction job initial state must be queued'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'extraction_jobs records cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW)
                    - 'status' - 'attempt_count' - 'result_checksum'
                    - 'error_code' - 'started_at' - 'finished_at'
                    - 'updated_at' - 'row_version')
                IS DISTINCT FROM
               (to_jsonb(OLD)
                    - 'status' - 'attempt_count' - 'result_checksum'
                    - 'error_code' - 'started_at' - 'finished_at'
                    - 'updated_at' - 'row_version') THEN
                RAISE EXCEPTION 'extraction job specification is immutable'
                    USING ERRCODE = '55000';
            END IF;
            old_status := OLD.status;
            new_status := NEW.status;
            IF new_status IS NOT DISTINCT FROM old_status AND
               (to_jsonb(NEW)
                    - 'updated_at' - 'row_version') IS DISTINCT FROM
               (to_jsonb(OLD)
                    - 'updated_at' - 'row_version') THEN
                RAISE EXCEPTION
                    'extraction job lifecycle fields require a state transition'
                    USING ERRCODE = '55000';
            END IF;
            IF new_status IS DISTINCT FROM old_status AND NOT (
                (old_status = 'queued' AND new_status = 'running') OR
                (old_status = 'running' AND new_status IN ('succeeded', 'failed')) OR
                (old_status = 'failed' AND new_status = 'running')
            ) THEN
                RAISE EXCEPTION 'invalid extraction job state transition'
                    USING ERRCODE = '55000';
            END IF;
            IF old_status IN ('queued', 'failed') AND new_status = 'running' AND
               NEW.attempt_count <> OLD.attempt_count + 1 THEN
                RAISE EXCEPTION 'starting extraction must increment attempt count once'
                    USING ERRCODE = '55000';
            END IF;
            IF old_status = 'running' AND new_status IN ('succeeded', 'failed') AND
               NEW.attempt_count <> OLD.attempt_count THEN
                RAISE EXCEPTION 'finishing extraction cannot change attempt count'
                    USING ERRCODE = '55000';
            END IF;
            IF old_status = 'running' AND new_status = 'succeeded' AND NOT EXISTS (
                SELECT 1
                  FROM extraction_proposal_artifacts
                 WHERE extraction_job_id = NEW.id
                   AND payload_checksum = NEW.result_checksum
            ) THEN
                RAISE EXCEPTION
                    'successful extraction requires its exact proposal artifact'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $magicforge$
        """
    )
    op.execute(
        """
        CREATE TRIGGER extraction_jobs_workflow_guard
        BEFORE INSERT OR UPDATE OR DELETE ON extraction_jobs
        FOR EACH ROW
        EXECUTE FUNCTION magicforge_guard_extraction_job()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $magicforge$
        BEGIN
            IF EXISTS (SELECT 1 FROM extraction_jobs) OR
               EXISTS (SELECT 1 FROM extraction_attempts) THEN
                RAISE EXCEPTION
                    '0007 downgrade would destroy Production extraction history'
                    USING ERRCODE = '55000';
            END IF;
        END;
        $magicforge$
        """
    )
    op.execute("DROP TRIGGER extraction_jobs_workflow_guard ON extraction_jobs")
    op.execute("DROP FUNCTION magicforge_guard_extraction_job()")
    op.execute(
        "DROP TRIGGER extraction_proposal_artifacts_insert_guard "
        "ON extraction_proposal_artifacts"
    )
    op.execute("DROP FUNCTION magicforge_guard_extraction_proposal_insert()")
    op.execute(
        "DROP TRIGGER extraction_proposal_artifacts_immutable "
        "ON extraction_proposal_artifacts"
    )
    op.execute("DROP TRIGGER extraction_attempts_immutable ON extraction_attempts")
    op.drop_index(
        "ix_extraction_proposals_source_created",
        table_name="extraction_proposal_artifacts",
    )
    op.drop_table("extraction_proposal_artifacts")
    op.drop_index(
        "ix_extraction_attempts_job_finished", table_name="extraction_attempts"
    )
    op.drop_table("extraction_attempts")
    op.drop_index("ix_extraction_jobs_review_created", table_name="extraction_jobs")
    op.drop_index("ix_extraction_jobs_source_status", table_name="extraction_jobs")
    op.drop_table("extraction_jobs")
    op.drop_constraint(
        "uq_source_review_id_version_permission",
        "source_review_decisions",
        type_="unique",
    )
    op.drop_constraint(
        "uq_source_versions_id_content_hash",
        "source_versions",
        type_="unique",
    )
