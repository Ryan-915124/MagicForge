"""Add persisted Mapping review and versioned knowledge artifacts.

Revision ID: 20260808_0003
Revises: 20260807_0002
Create Date: 2026-08-08
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260808_0003"
down_revision: str | None = "20260807_0002"
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


MAPPING_PROPOSAL_KIND = _enum(
    "mapping_proposal_kind",
    "entity",
    "relationship",
)
MAPPING_PROPOSAL_STATUS = _enum(
    "mapping_proposal_workflow_status",
    "submitted",
    "approved",
    "rejected",
    "superseded",
    "revoked",
)
MAPPING_SENSITIVITY = _enum(
    "mapping_proposal_sensitivity",
    "public",
    "controlled",
    "secret_method",
    "restricted",
)
MAPPING_VALIDATION_PHASE = _enum(
    "mapping_validation_phase",
    "review",
    "manifest_authorization",
)
MAPPING_REVIEW_DECISION = _enum(
    "mapping_review_decision_kind",
    "approved",
    "rejected",
    "corrected",
    "superseded",
    "revoked",
)
MAPPING_REVIEW_STATUS = _enum(
    "mapping_review_resulting_status",
    "approved",
    "rejected",
    "superseded",
    "revoked",
)
KNOWLEDGE_ENTITY_TYPE = _enum(
    "knowledge_entity_type",
    "effect",
    "technique",
    "method",
    "psychology_principle",
    "performer",
    "source",
    "cognitive_mechanism",
    "research_paper",
)
CANONICAL_ENTITY_STATUS = _enum(
    "canonical_entity_status",
    "active",
    "merged",
)
KNOWLEDGE_RELATIONSHIP_TYPE = _enum(
    "knowledge_relationship_type",
    "uses",
    "inspired_by",
    "requires",
    "explains",
    "performed_by",
    "related_to",
)


IMMUTABLE_TABLES = (
    "mapping_proposal_evidence",
    "mapping_validation_runs",
    "mapping_review_decisions",
    "knowledge_node_versions",
    "knowledge_relationships",
    "knowledge_relationship_assertions",
)


def upgrade() -> None:
    op.create_table(
        "mapping_proposals",
        sa.Column("id", UUID, nullable=False),
        sa.Column("proposal_kind", MAPPING_PROPOSAL_KIND, nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("validation_rule_version", sa.String(length=255), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("proposal_checksum", sa.String(length=64), nullable=False),
        sa.Column("subject_entity_id", UUID, nullable=True),
        sa.Column("sensitivity", MAPPING_SENSITIVITY, nullable=False),
        sa.Column("proposer_user_id", UUID, nullable=True),
        sa.Column("proposer_run_id", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            MAPPING_PROPOSAL_STATUS,
            server_default="submitted",
            nullable=False,
        ),
        sa.Column("approved_artifact_id", UUID, nullable=True),
        sa.Column("approved_artifact_version", sa.Integer(), nullable=True),
        sa.Column("supersedes_proposal_id", UUID, nullable=True),
        sa.Column(
            "submitted_at",
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
            "proposal_kind IN ('entity', 'relationship')",
            name="mapping_proposal_kind",
        ),
        sa.CheckConstraint(
            "status IN ('submitted', 'approved', 'rejected', 'superseded', 'revoked')",
            name="mapping_proposal_workflow_status",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('public', 'controlled', 'secret_method', 'restricted')",
            name="mapping_proposal_sensitivity",
        ),
        sa.CheckConstraint(
            "length(trim(schema_version)) > 0",
            name="ck_mapping_proposals_schema_present",
        ),
        sa.CheckConstraint(
            "length(trim(validation_rule_version)) > 0",
            name="ck_mapping_proposals_rule_present",
        ),
        sa.CheckConstraint(
            "length(proposal_checksum) = 64",
            name="ck_mapping_proposals_checksum",
        ),
        sa.CheckConstraint(
            "proposer_user_id IS NOT NULL OR "
            "(proposer_run_id IS NOT NULL AND length(trim(proposer_run_id)) > 0)",
            name="ck_mapping_proposals_proposer_present",
        ),
        sa.CheckConstraint(
            "supersedes_proposal_id IS NULL OR supersedes_proposal_id <> id",
            name="ck_mapping_proposals_no_self_supersession",
        ),
        sa.CheckConstraint(
            "row_version > 0",
            name="ck_mapping_proposals_row_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["proposer_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_proposal_id"],
            ["mapping_proposals.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mapping_proposals"),
        sa.UniqueConstraint(
            "proposal_kind",
            "proposal_checksum",
            name="uq_mapping_proposals_kind_checksum",
        ),
    )
    op.create_index(
        "ix_mapping_proposals_status_submitted",
        "mapping_proposals",
        ["status", "submitted_at"],
        unique=False,
    )
    op.create_index(
        "ix_mapping_proposals_proposer_status",
        "mapping_proposals",
        ["proposer_user_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_mapping_proposals_subject",
        "mapping_proposals",
        ["subject_entity_id"],
        unique=False,
    )

    op.create_table(
        "mapping_proposal_evidence",
        sa.Column("mapping_proposal_id", UUID, nullable=False),
        sa.Column("evidence_card_version_id", UUID, nullable=False),
        sa.Column("evidence_domain_object_id", UUID, nullable=False),
        sa.Column("evidence_payload_checksum", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(evidence_payload_checksum) = 64",
            name="ck_mapping_proposal_evidence_checksum",
        ),
        sa.ForeignKeyConstraint(
            ["mapping_proposal_id"],
            ["mapping_proposals.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_card_version_id"],
            ["evidence_card_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "mapping_proposal_id",
            "evidence_card_version_id",
            name="pk_mapping_proposal_evidence",
        ),
    )
    op.create_index(
        "ix_mapping_proposal_evidence_version",
        "mapping_proposal_evidence",
        ["evidence_card_version_id"],
        unique=False,
    )

    op.create_table(
        "mapping_validation_runs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("mapping_proposal_id", UUID, nullable=False),
        sa.Column("phase", MAPPING_VALIDATION_PHASE, nullable=False),
        sa.Column("rule_version", sa.String(length=255), nullable=False),
        sa.Column("rule_versions", JSONB, nullable=False),
        sa.Column("input_checksum", sa.String(length=64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("results", JSONB, nullable=False),
        sa.Column("run_checksum", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "phase IN ('review', 'manifest_authorization')",
            name="mapping_validation_phase",
        ),
        sa.CheckConstraint(
            "length(trim(rule_version)) > 0",
            name="ck_mapping_validation_rule_present",
        ),
        sa.CheckConstraint(
            "length(input_checksum) = 64",
            name="ck_mapping_validation_input_checksum",
        ),
        sa.CheckConstraint(
            "length(run_checksum) = 64",
            name="ck_mapping_validation_run_checksum",
        ),
        sa.ForeignKeyConstraint(
            ["mapping_proposal_id"],
            ["mapping_proposals.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mapping_validation_runs"),
    )
    op.create_index(
        "ix_mapping_validation_proposal_phase",
        "mapping_validation_runs",
        ["mapping_proposal_id", "phase"],
        unique=False,
    )

    op.create_table(
        "mapping_review_decisions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("mapping_proposal_id", UUID, nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("decision", MAPPING_REVIEW_DECISION, nullable=False),
        sa.Column("resulting_status", MAPPING_REVIEW_STATUS, nullable=False),
        sa.Column("reviewer_user_id", UUID, nullable=False),
        sa.Column("actor_role_snapshot", JSONB, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("confidence", JSONB, nullable=True),
        sa.Column("canonical_resolution", sa.String(length=40), nullable=True),
        sa.Column("canonical_entity_id", UUID, nullable=True),
        sa.Column("validation_run_id", UUID, nullable=True),
        sa.Column("approved_artifact_id", UUID, nullable=True),
        sa.Column("approved_artifact_version", sa.Integer(), nullable=True),
        sa.Column("decision_checksum", sa.String(length=64), nullable=False),
        sa.Column("supersedes_decision_id", UUID, nullable=True),
        sa.Column("policy_schema_version", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'corrected', 'superseded', 'revoked')",
            name="mapping_review_decision_kind",
        ),
        sa.CheckConstraint(
            "resulting_status IN ('approved', 'rejected', 'superseded', 'revoked')",
            name="mapping_review_resulting_status",
        ),
        sa.CheckConstraint(
            "sequence_number > 0",
            name="ck_mapping_review_sequence_positive",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_mapping_review_reason_present",
        ),
        sa.CheckConstraint(
            "length(decision_checksum) = 64",
            name="ck_mapping_review_checksum",
        ),
        sa.CheckConstraint(
            "decision <> 'approved' OR "
            "(resulting_status = 'approved' AND validation_run_id IS NOT NULL AND "
            "approved_artifact_id IS NOT NULL AND approved_artifact_version IS NOT NULL)",
            name="ck_mapping_review_approval_requirements",
        ),
        sa.ForeignKeyConstraint(
            ["mapping_proposal_id"],
            ["mapping_proposals.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["validation_run_id"],
            ["mapping_validation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision_id"],
            ["mapping_review_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mapping_review_decisions"),
        sa.UniqueConstraint(
            "mapping_proposal_id",
            "sequence_number",
            name="uq_mapping_review_proposal_sequence",
        ),
        sa.UniqueConstraint(
            "mapping_proposal_id",
            "decision_checksum",
            name="uq_mapping_review_proposal_checksum",
        ),
    )
    op.create_index(
        "ix_mapping_review_proposal_created",
        "mapping_review_decisions",
        ["mapping_proposal_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_mapping_review_reviewer_created",
        "mapping_review_decisions",
        ["reviewer_user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "knowledge_entities",
        sa.Column("id", UUID, nullable=False),
        sa.Column("entity_type", KNOWLEDGE_ENTITY_TYPE, nullable=False),
        sa.Column("canonical_name", sa.String(length=512), nullable=False),
        sa.Column("canonical_key", sa.String(length=512), nullable=False),
        sa.Column("aliases", JSONB, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("attributes", JSONB, nullable=False),
        sa.Column(
            "status",
            CANONICAL_ENTITY_STATUS,
            server_default="active",
            nullable=False,
        ),
        sa.Column("merged_into_entity_id", UUID, nullable=True),
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
        sa.CheckConstraint(
            "entity_type IN ('effect', 'technique', 'method', 'psychology_principle', "
            "'performer', 'source', 'cognitive_mechanism', 'research_paper')",
            name="knowledge_entity_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'merged')",
            name="canonical_entity_status",
        ),
        sa.CheckConstraint(
            "length(trim(canonical_key)) > 0",
            name="ck_knowledge_entities_key_present",
        ),
        sa.CheckConstraint(
            "length(trim(canonical_name)) > 0",
            name="ck_knowledge_entities_name_present",
        ),
        sa.CheckConstraint(
            "(status = 'merged' AND merged_into_entity_id IS NOT NULL) OR "
            "(status = 'active' AND merged_into_entity_id IS NULL)",
            name="ck_knowledge_entities_merge_state",
        ),
        sa.CheckConstraint(
            "merged_into_entity_id IS NULL OR merged_into_entity_id <> id",
            name="ck_knowledge_entities_no_self_merge",
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_entity_id"],
            ["knowledge_entities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_entities"),
        sa.UniqueConstraint(
            "canonical_key", name="uq_knowledge_entities_canonical_key"
        ),
    )
    op.create_index(
        "ix_knowledge_entities_type_status",
        "knowledge_entities",
        ["entity_type", "status"],
        unique=False,
    )

    op.create_table(
        "knowledge_node_versions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("entity_id", UUID, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("payload_checksum", sa.String(length=64), nullable=False),
        sa.Column("mapping_proposal_id", UUID, nullable=False),
        sa.Column("mapping_review_decision_id", UUID, nullable=False),
        sa.Column("supersedes_version_id", UUID, nullable=True),
        sa.Column("created_by_user_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version_number > 0",
            name="ck_knowledge_node_versions_positive",
        ),
        sa.CheckConstraint(
            "length(payload_checksum) = 64",
            name="ck_knowledge_node_versions_checksum",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["knowledge_entities.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["mapping_proposal_id"],
            ["mapping_proposals.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mapping_review_decision_id"],
            ["mapping_review_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_version_id"],
            ["knowledge_node_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_node_versions"),
        sa.UniqueConstraint(
            "entity_id",
            "version_number",
            name="uq_knowledge_node_entity_version",
        ),
        sa.UniqueConstraint(
            "mapping_review_decision_id",
            name="uq_knowledge_node_review_decision",
        ),
    )
    op.create_index(
        "ix_knowledge_node_entity_created",
        "knowledge_node_versions",
        ["entity_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "knowledge_relationships",
        sa.Column("id", UUID, nullable=False),
        sa.Column("source_entity_id", UUID, nullable=False),
        sa.Column("target_entity_id", UUID, nullable=False),
        sa.Column("relation_type", KNOWLEDGE_RELATIONSHIP_TYPE, nullable=False),
        sa.Column("canonical_key", sa.String(length=1200), nullable=False),
        sa.Column("attributes", JSONB, nullable=False),
        sa.Column("created_by_user_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "relation_type IN ('uses', 'inspired_by', 'requires', 'explains', "
            "'performed_by', 'related_to')",
            name="knowledge_relationship_type",
        ),
        sa.CheckConstraint(
            "source_entity_id <> target_entity_id",
            name="ck_knowledge_relationship_no_self",
        ),
        sa.ForeignKeyConstraint(
            ["source_entity_id"],
            ["knowledge_entities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_entity_id"],
            ["knowledge_entities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_relationships"),
        sa.UniqueConstraint(
            "canonical_key", name="uq_knowledge_relationship_canonical_key"
        ),
        sa.UniqueConstraint(
            "source_entity_id",
            "relation_type",
            "target_entity_id",
            name="uq_knowledge_relationship_endpoints_type",
        ),
    )
    op.create_index(
        "ix_knowledge_relationship_source",
        "knowledge_relationships",
        ["source_entity_id", "relation_type"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_relationship_target",
        "knowledge_relationships",
        ["target_entity_id", "relation_type"],
        unique=False,
    )

    op.create_table(
        "knowledge_relationship_assertions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("relationship_id", UUID, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=80), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("projection_context", JSONB, nullable=False),
        sa.Column("payload_checksum", sa.String(length=64), nullable=False),
        sa.Column("mapping_proposal_id", UUID, nullable=False),
        sa.Column("mapping_review_decision_id", UUID, nullable=False),
        sa.Column("supersedes_assertion_id", UUID, nullable=True),
        sa.Column("created_by_user_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version_number > 0",
            name="ck_relationship_assertions_positive",
        ),
        sa.CheckConstraint(
            "length(payload_checksum) = 64",
            name="ck_relationship_assertions_checksum",
        ),
        sa.ForeignKeyConstraint(
            ["relationship_id"],
            ["knowledge_relationships.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mapping_proposal_id"],
            ["mapping_proposals.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["mapping_review_decision_id"],
            ["mapping_review_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_assertion_id"],
            ["knowledge_relationship_assertions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint(
            "id", name="pk_knowledge_relationship_assertions"
        ),
        sa.UniqueConstraint(
            "relationship_id",
            "version_number",
            name="uq_relationship_assertion_version",
        ),
        sa.UniqueConstraint(
            "mapping_review_decision_id",
            name="uq_relationship_assertion_review_decision",
        ),
    )
    op.create_index(
        "ix_relationship_assertion_relationship_created",
        "knowledge_relationship_assertions",
        ["relationship_id", "created_at"],
        unique=False,
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
        CREATE FUNCTION magicforge_guard_mapping_workflow()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $magicforge$
        DECLARE
            latest_status text;
            latest_artifact_id uuid;
            latest_artifact_version integer;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.status <> 'submitted' THEN
                    RAISE EXCEPTION
                        'mapping proposal initial status must be submitted'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'mapping proposal records cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - 'status' - 'approved_artifact_id' -
                    'approved_artifact_version' - 'updated_at' - 'row_version')
                IS DISTINCT FROM
               (to_jsonb(OLD) - 'status' - 'approved_artifact_id' -
                    'approved_artifact_version' - 'updated_at' - 'row_version') THEN
                RAISE EXCEPTION 'mapping proposal payload fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status OR
               NEW.approved_artifact_id IS DISTINCT FROM OLD.approved_artifact_id OR
               NEW.approved_artifact_version IS DISTINCT FROM OLD.approved_artifact_version THEN
                SELECT resulting_status, approved_artifact_id, approved_artifact_version
                  INTO latest_status, latest_artifact_id, latest_artifact_version
                  FROM mapping_review_decisions
                 WHERE mapping_proposal_id = OLD.id
                 ORDER BY sequence_number DESC
                 LIMIT 1;
                IF latest_status IS DISTINCT FROM NEW.status THEN
                    RAISE EXCEPTION
                        'mapping proposal status must match the latest review decision'
                        USING ERRCODE = '55000';
                END IF;
                IF NEW.status = 'approved' AND
                   (latest_artifact_id IS DISTINCT FROM NEW.approved_artifact_id OR
                    latest_artifact_version IS DISTINCT FROM NEW.approved_artifact_version) THEN
                    RAISE EXCEPTION
                        'mapping proposal artifact must match the latest review decision'
                        USING ERRCODE = '55000';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $magicforge$
        """
    )
    op.execute(
        """
        CREATE TRIGGER mapping_proposals_workflow_guard
        BEFORE INSERT OR UPDATE OR DELETE ON mapping_proposals
        FOR EACH ROW
        EXECUTE FUNCTION magicforge_guard_mapping_workflow()
        """
    )

    op.execute(
        """
        CREATE FUNCTION magicforge_guard_knowledge_entity_identity()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $magicforge$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'knowledge entity records cannot be deleted'
                    USING ERRCODE = '55000';
            END IF;
            IF (to_jsonb(NEW) - 'aliases' - 'status' - 'merged_into_entity_id' -
                    'updated_at') IS DISTINCT FROM
               (to_jsonb(OLD) - 'aliases' - 'status' - 'merged_into_entity_id' -
                    'updated_at') THEN
                RAISE EXCEPTION 'knowledge entity identity fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $magicforge$
        """
    )
    op.execute(
        """
        CREATE TRIGGER knowledge_entities_identity_guard
        BEFORE UPDATE OR DELETE ON knowledge_entities
        FOR EACH ROW
        EXECUTE FUNCTION magicforge_guard_knowledge_entity_identity()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER knowledge_entities_identity_guard ON knowledge_entities"
    )
    op.execute("DROP FUNCTION magicforge_guard_knowledge_entity_identity()")
    op.execute(
        "DROP TRIGGER mapping_proposals_workflow_guard ON mapping_proposals"
    )
    op.execute("DROP FUNCTION magicforge_guard_mapping_workflow()")

    for table_name in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER {table_name}_immutable ON {table_name}")

    op.drop_index(
        "ix_relationship_assertion_relationship_created",
        table_name="knowledge_relationship_assertions",
    )
    op.drop_table("knowledge_relationship_assertions")

    op.drop_index(
        "ix_knowledge_relationship_target", table_name="knowledge_relationships"
    )
    op.drop_index(
        "ix_knowledge_relationship_source", table_name="knowledge_relationships"
    )
    op.drop_table("knowledge_relationships")

    op.drop_index(
        "ix_knowledge_node_entity_created", table_name="knowledge_node_versions"
    )
    op.drop_table("knowledge_node_versions")

    op.drop_index(
        "ix_knowledge_entities_type_status", table_name="knowledge_entities"
    )
    op.drop_table("knowledge_entities")

    op.drop_index(
        "ix_mapping_review_reviewer_created",
        table_name="mapping_review_decisions",
    )
    op.drop_index(
        "ix_mapping_review_proposal_created",
        table_name="mapping_review_decisions",
    )
    op.drop_table("mapping_review_decisions")

    op.drop_index(
        "ix_mapping_validation_proposal_phase",
        table_name="mapping_validation_runs",
    )
    op.drop_table("mapping_validation_runs")

    op.drop_index(
        "ix_mapping_proposal_evidence_version",
        table_name="mapping_proposal_evidence",
    )
    op.drop_table("mapping_proposal_evidence")

    op.drop_index("ix_mapping_proposals_subject", table_name="mapping_proposals")
    op.drop_index(
        "ix_mapping_proposals_proposer_status", table_name="mapping_proposals"
    )
    op.drop_index(
        "ix_mapping_proposals_status_submitted", table_name="mapping_proposals"
    )
    op.drop_table("mapping_proposals")
