"""Persist append-only Source permission requests.

Revision ID: 20260808_0006
Revises: 20260808_0005
Create Date: 2026-08-08

Every immutable SourceVersion receives an explicit permission-request history.
Legacy access metadata is copied into a fail-closed sequence-1 request: missing
or malformed permissions become ``none`` and the migration itself grants no
new extraction or storage authority.  Existing Source review decisions are
then bound to the request for their exact SourceVersion.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "20260808_0006"
down_revision: str | None = "20260808_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SOURCE_PERMISSION_EXTRACTION = sa.String(length=17)
SOURCE_PERMISSION_STORAGE = sa.String(length=26)


def upgrade() -> None:
    # Source review history is immutable.  This migration deliberately refuses
    # to rewrite old decisions to add a permission-request link.  Production
    # must either have no Source decisions (the current rollout state) or use a
    # separately reviewed migration that can preserve their original audit
    # semantics and checksums.
    op.execute(
        """
        DO $magicforge$
        BEGIN
            IF EXISTS (SELECT 1 FROM source_review_decisions) THEN
                RAISE EXCEPTION
                    '0006 requires empty source_review_decisions; immutable history migration required'
                    USING ERRCODE = '55000';
            END IF;
        END;
        $magicforge$
        """
    )

    op.create_table(
        "source_permission_requests",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_version_id", UUID(as_uuid=True), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column(
            "requested_extraction_permission",
            SOURCE_PERMISSION_EXTRACTION,
            nullable=False,
        ),
        sa.Column(
            "requested_storage_permission",
            SOURCE_PERMISSION_STORAGE,
            nullable=False,
        ),
        sa.Column("requested_scope_locators", JSONB, nullable=False),
        sa.Column("rights_basis", sa.Text(), nullable=False),
        sa.Column("rights_evidence", JSONB, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("submitted_by_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("actor_role_snapshot", JSONB, nullable=False),
        sa.Column(
            "domain_schema_version",
            sa.String(length=80),
            server_default="source-permission-request-0.1",
            nullable=False,
        ),
        sa.Column("request_checksum", sa.String(length=64), nullable=False),
        sa.Column("supersedes_request_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "requested_extraction_permission IN "
            "('none', 'metadata_only', 'selected_sections', 'full_text')",
            name="source_permission_request_extraction_permission",
        ),
        sa.CheckConstraint(
            "requested_storage_permission IN "
            "('none', 'derived_knowledge_only', 'derived_with_short_excerpt')",
            name="source_permission_request_storage_permission",
        ),
        sa.CheckConstraint(
            "sequence_number > 0",
            name="ck_source_permission_request_sequence_positive",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(requested_scope_locators) = 'array'",
            name="ck_source_permission_request_scope_array",
        ),
        sa.CheckConstraint(
            "length(trim(rights_basis)) > 0",
            name="ck_source_permission_request_rights_basis_present",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rights_evidence) = 'array' AND "
            "jsonb_array_length(rights_evidence) > 0",
            name="ck_source_permission_request_rights_evidence_nonempty_array",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_source_permission_request_reason_present",
        ),
        sa.CheckConstraint(
            "length(trim(domain_schema_version)) > 0",
            name="ck_source_permission_request_schema_present",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(actor_role_snapshot) = 'array'",
            name="ck_source_permission_request_actor_roles_array",
        ),
        sa.CheckConstraint(
            "length(request_checksum) = 64",
            name="ck_source_permission_request_checksum",
        ),
        sa.CheckConstraint(
            "supersedes_request_id IS NULL OR supersedes_request_id <> id",
            name="ck_source_permission_request_no_self_supersession",
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id"],
            ["source_versions.id"],
            name="fk_source_permission_request_source_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"],
            ["users.id"],
            name="fk_source_permission_request_submitter",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_request_id", "source_version_id"],
            [
                "source_permission_requests.id",
                "source_permission_requests.source_version_id",
            ],
            name="fk_source_permission_request_supersedes_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_permission_requests"),
        sa.UniqueConstraint(
            "source_version_id",
            "sequence_number",
            name="uq_source_permission_request_version_sequence",
        ),
        sa.UniqueConstraint(
            "source_version_id",
            "request_checksum",
            name="uq_source_permission_request_version_checksum",
        ),
        sa.UniqueConstraint(
            "id",
            "source_version_id",
            name="uq_source_permission_request_id_version",
        ),
    )
    op.create_index(
        "ix_source_permission_request_version_created",
        "source_permission_requests",
        ["source_version_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_permission_request_submitter_created",
        "source_permission_requests",
        ["submitted_by_user_id", "created_at"],
        unique=False,
    )

    op.add_column(
        "source_review_decisions",
        sa.Column("source_permission_request_id", UUID(as_uuid=True), nullable=True),
    )

    op.execute(
        """
        WITH normalized AS (
            SELECT
                source_versions.id AS source_version_id,
                source_versions.submitted_by_user_id,
                source_versions.created_at,
                source_versions.content_hash,
                source_versions.access_metadata,
                CASE
                    WHEN source_versions.access_metadata
                        ->> 'requested_extraction_permission' IN (
                            'none', 'metadata_only', 'selected_sections', 'full_text'
                        )
                    THEN source_versions.access_metadata
                        ->> 'requested_extraction_permission'
                    ELSE 'none'
                END AS requested_extraction_permission,
                CASE
                    WHEN source_versions.access_metadata
                        ->> 'requested_storage_permission' IN (
                            'none', 'derived_knowledge_only',
                            'derived_with_short_excerpt'
                        )
                    THEN source_versions.access_metadata
                        ->> 'requested_storage_permission'
                    ELSE 'none'
                END AS requested_storage_permission,
                CASE
                    WHEN jsonb_typeof(
                        source_versions.access_metadata
                            -> 'requested_scope_locators'
                    ) = 'array'
                    THEN source_versions.access_metadata
                        -> 'requested_scope_locators'
                    ELSE '[]'::jsonb
                END AS requested_scope_locators
            FROM source_versions
        ),
        backfill AS (
            SELECT
                source_version_id,
                submitted_by_user_id,
                created_at,
                content_hash,
                requested_extraction_permission,
                requested_storage_permission,
                requested_scope_locators,
                'legacy_access_metadata_snapshot_only_no_grant'::text
                    AS rights_basis,
                jsonb_build_array(
                    'source-version:' || source_version_id::text,
                    'content-sha256:' || content_hash
                ) AS rights_evidence,
                'Backfilled from immutable SourceVersion access_metadata; no permission elevation.'::text
                    AS reason
            FROM normalized
        )
        INSERT INTO source_permission_requests (
            id,
            source_version_id,
            sequence_number,
            requested_extraction_permission,
            requested_storage_permission,
            requested_scope_locators,
            rights_basis,
            rights_evidence,
            reason,
            submitted_by_user_id,
            actor_role_snapshot,
            domain_schema_version,
            request_checksum,
            supersedes_request_id,
            created_at
        )
        SELECT
            (
                substr(md5('magicforge:source-permission-request:' ||
                    source_version_id::text), 1, 8) || '-' ||
                substr(md5('magicforge:source-permission-request:' ||
                    source_version_id::text), 9, 4) || '-' ||
                substr(md5('magicforge:source-permission-request:' ||
                    source_version_id::text), 13, 4) || '-' ||
                substr(md5('magicforge:source-permission-request:' ||
                    source_version_id::text), 17, 4) || '-' ||
                substr(md5('magicforge:source-permission-request:' ||
                    source_version_id::text), 21, 12)
            )::uuid,
            source_version_id,
            1,
            requested_extraction_permission,
            requested_storage_permission,
            requested_scope_locators,
            rights_basis,
            rights_evidence,
            reason,
            submitted_by_user_id,
            '[]'::jsonb,
            'source-permission-request-legacy-0.1',
            encode(
                sha256(
                    convert_to(
                        'magicforge:legacy-source-permission-request:' ||
                            source_version_id::text || ':' || content_hash,
                        'UTF8'
                    )
                ),
                'hex'
            ),
            NULL,
            created_at
        FROM backfill
        """
    )

    op.alter_column(
        "source_review_decisions",
        "source_permission_request_id",
        existing_type=UUID(as_uuid=True),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_source_review_permission_request_version",
        "source_review_decisions",
        "source_permission_requests",
        ["source_permission_request_id", "source_version_id"],
        ["id", "source_version_id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint(
        "ck_source_review_approval_requirements",
        "source_review_decisions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_source_review_approval_requirements",
        "source_review_decisions",
        "resulting_status <> 'approved' OR "
        "(source_permission_request_id IS NOT NULL AND "
        "citation_verification_evidence_id IS NOT NULL AND "
        "claim_eligibility IN ('eligible', 'eligible_with_limits') AND "
        "extraction_permission IN ('selected_sections', 'full_text'))",
    )

    op.execute(
        """
        CREATE TRIGGER source_permission_requests_immutable
        BEFORE UPDATE OR DELETE ON source_permission_requests
        FOR EACH ROW
        EXECUTE FUNCTION magicforge_reject_governance_record_mutation()
        """
    )


def downgrade() -> None:
    # A downgrade removes the request table and the decision-to-request link.
    # That is lossless only while the table is still the exact deterministic
    # backfill created by this migration.  Any runtime request or altered
    # legacy marker represents Production history that the 0005 schema cannot
    # preserve, so fail closed before executing destructive DDL.
    op.execute(
        """
        DO $magicforge$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM source_permission_requests AS request
                JOIN source_versions AS version
                  ON version.id = request.source_version_id
                WHERE request.domain_schema_version IS DISTINCT FROM
                        'source-permission-request-legacy-0.1'
                   OR request.sequence_number IS DISTINCT FROM 1
                   OR request.supersedes_request_id IS NOT NULL
                   OR request.submitted_by_user_id IS DISTINCT FROM
                        version.submitted_by_user_id
                   OR request.actor_role_snapshot IS DISTINCT FROM '[]'::jsonb
                   OR request.requested_extraction_permission IS DISTINCT FROM
                        CASE
                            WHEN version.access_metadata
                                ->> 'requested_extraction_permission' IN (
                                    'none', 'metadata_only',
                                    'selected_sections', 'full_text'
                                )
                            THEN version.access_metadata
                                ->> 'requested_extraction_permission'
                            ELSE 'none'
                        END
                   OR request.requested_storage_permission IS DISTINCT FROM
                        CASE
                            WHEN version.access_metadata
                                ->> 'requested_storage_permission' IN (
                                    'none', 'derived_knowledge_only',
                                    'derived_with_short_excerpt'
                                )
                            THEN version.access_metadata
                                ->> 'requested_storage_permission'
                            ELSE 'none'
                        END
                   OR request.requested_scope_locators IS DISTINCT FROM
                        CASE
                            WHEN jsonb_typeof(
                                version.access_metadata
                                    -> 'requested_scope_locators'
                            ) = 'array'
                            THEN version.access_metadata
                                -> 'requested_scope_locators'
                            ELSE '[]'::jsonb
                        END
                   OR request.rights_basis IS DISTINCT FROM
                        'legacy_access_metadata_snapshot_only_no_grant'
                   OR request.rights_evidence IS DISTINCT FROM
                        jsonb_build_array(
                            'source-version:' || version.id::text,
                            'content-sha256:' || version.content_hash
                        )
                   OR request.reason IS DISTINCT FROM
                        'Backfilled from immutable SourceVersion access_metadata; no permission elevation.'
                   OR request.request_checksum IS DISTINCT FROM
                        encode(
                            sha256(
                                convert_to(
                                    'magicforge:legacy-source-permission-request:' ||
                                        version.id::text || ':' ||
                                        version.content_hash,
                                    'UTF8'
                                )
                            ),
                            'hex'
                        )
                   OR request.id IS DISTINCT FROM (
                        substr(md5('magicforge:source-permission-request:' ||
                            version.id::text), 1, 8) || '-' ||
                        substr(md5('magicforge:source-permission-request:' ||
                            version.id::text), 9, 4) || '-' ||
                        substr(md5('magicforge:source-permission-request:' ||
                            version.id::text), 13, 4) || '-' ||
                        substr(md5('magicforge:source-permission-request:' ||
                            version.id::text), 17, 4) || '-' ||
                        substr(md5('magicforge:source-permission-request:' ||
                            version.id::text), 21, 12)
                   )::uuid
                   OR request.created_at IS DISTINCT FROM version.created_at
            ) OR EXISTS (
                SELECT 1
                FROM source_review_decisions
            ) OR EXISTS (
                SELECT 1
                FROM source_versions AS version
                LEFT JOIN source_permission_requests AS request
                  ON request.source_version_id = version.id
                WHERE request.id IS NULL
            ) OR EXISTS (
                SELECT 1
                FROM source_permission_requests AS request
                LEFT JOIN source_versions AS version
                  ON version.id = request.source_version_id
                WHERE version.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'source permission history is not pure 0006 legacy backfill; downgrade refused'
                    USING ERRCODE = '55000';
            END IF;
        END;
        $magicforge$
        """
    )

    op.drop_constraint(
        "ck_source_review_approval_requirements",
        "source_review_decisions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_source_review_approval_requirements",
        "source_review_decisions",
        "resulting_status <> 'approved' OR "
        "(citation_verification_evidence_id IS NOT NULL AND "
        "claim_eligibility IN ('eligible', 'eligible_with_limits') AND "
        "extraction_permission IN ('selected_sections', 'full_text'))",
    )
    op.drop_constraint(
        "fk_source_review_permission_request_version",
        "source_review_decisions",
        type_="foreignkey",
    )
    op.drop_column("source_review_decisions", "source_permission_request_id")

    op.execute(
        "DROP TRIGGER source_permission_requests_immutable "
        "ON source_permission_requests"
    )
    op.drop_index(
        "ix_source_permission_request_submitter_created",
        table_name="source_permission_requests",
    )
    op.drop_index(
        "ix_source_permission_request_version_created",
        table_name="source_permission_requests",
    )
    op.drop_table("source_permission_requests")
