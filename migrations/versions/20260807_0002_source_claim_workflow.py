"""Add persisted Source and Claim governance workflows.

Revision ID: 20260807_0002
Revises: 20260807_0001
Create Date: 2026-08-07
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260807_0002"
down_revision: str | None = "20260807_0001"
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


SOURCE_CONTENT_ACCESS = _enum(
    "source_version_content_access",
    "search_snippet",
    "abstract",
    "web_extract",
    "full_text",
    "pdf_text",
)
SOURCE_TYPE = _enum(
    "source_version_source_type",
    "journal_article",
    "conference_paper",
    "preprint",
    "academic_book",
    "book_chapter",
    "practitioner_book",
    "web_article",
    "interview",
    "transcript",
    "archival_material",
    "internal_analysis",
)
SOURCE_ORIGIN = _enum(
    "source_version_knowledge_origin",
    "scientific_evidence",
    "expert_practice",
    "personal_interpretation",
)
SOURCE_SENSITIVITY = _enum(
    "source_version_sensitivity",
    "public",
    "controlled",
    "secret_method",
    "restricted",
)
CITATION_METHOD = _enum(
    "citation_verification_method",
    "metadata_resolver",
    "canonical_locator",
    "full_text_locator",
    "manual_review",
    "legacy_migration",
)
CITATION_SCOPE = _enum(
    "citation_verification_scope",
    "metadata",
    "full_text",
)
DECISION_VALUES = (
    "approved",
    "rejected",
    "corrected",
    "superseded",
    "revoked",
)
WORKFLOW_VALUES = (
    "draft",
    "submitted",
    "under_review",
    "approved",
    "rejected",
    "superseded",
    "revoked",
)
SOURCE_REVIEW_DECISION = _enum("source_review_decision_kind", *DECISION_VALUES)
SOURCE_REVIEW_STATUS = _enum("source_review_resulting_status", *WORKFLOW_VALUES)
SOURCE_REVIEW_ELIGIBILITY = _enum(
    "source_review_claim_eligibility",
    "not_assessed",
    "eligible",
    "eligible_with_limits",
    "ineligible",
)
SOURCE_REVIEW_EXTRACTION = _enum(
    "source_review_extraction_permission",
    "none",
    "metadata_only",
    "selected_sections",
    "full_text",
)
SOURCE_REVIEW_STORAGE = _enum(
    "source_review_storage_permission",
    "none",
    "derived_knowledge_only",
    "derived_with_short_excerpt",
)
SOURCE_REVIEW_SENSITIVITY = _enum(
    "source_review_sensitivity",
    "public",
    "controlled",
    "secret_method",
    "restricted",
)
SOURCE_REVIEW_CONTRADICTION = _enum(
    "source_review_contradiction_check",
    "not_checked",
    "checked_none_found",
    "checked_conflicts_linked",
    "not_applicable",
)
CLAIM_ROLE = _enum(
    "claim_candidate_role",
    "result",
    "method",
    "background",
    "hypothesis",
    "discussion",
    "expert_opinion",
    "context_only",
)
CLAIM_POLARITY = _enum(
    "claim_candidate_polarity", "supports", "contradicts", "qualifies"
)
CLAIM_CREATOR = _enum(
    "claim_candidate_created_by_kind", "human", "glm", "pipeline"
)
CLAIM_STATUS = _enum("claim_candidate_workflow_status", *WORKFLOW_VALUES)
CLAIM_REVIEW_DECISION = _enum("claim_review_decision_kind", *DECISION_VALUES)
CLAIM_REVIEW_STATUS = _enum("claim_review_resulting_status", *WORKFLOW_VALUES)
CLAIM_REVIEW_CLASS = _enum(
    "claim_review_evidence_class",
    "controlled_experiment",
    "quasi_experiment",
    "observational_study",
    "systematic_review",
    "meta_analysis",
    "narrative_review",
    "expert_instruction",
    "expert_case_analysis",
    "practitioner_report",
    "historical_primary_record",
    "historical_secondary_analysis",
    "analyst_interpretation",
    "anecdotal_observation",
)
CLAIM_REVIEW_ORIGIN = _enum(
    "claim_review_knowledge_origin",
    "scientific_evidence",
    "expert_practice",
    "personal_interpretation",
)
CLAIM_REVIEW_LEVEL = _enum(
    "claim_review_evidence_level",
    "empirical",
    "review",
    "practitioner",
    "anecdotal",
)
CLAIM_REVIEW_ELIGIBILITY = _enum(
    "claim_review_claim_eligibility",
    "not_assessed",
    "eligible",
    "eligible_with_limits",
    "ineligible",
)
CLAIM_REVIEW_STORAGE = _enum(
    "claim_review_storage_permission",
    "none",
    "derived_knowledge_only",
    "derived_with_short_excerpt",
)
CLAIM_REVIEW_CONTRADICTION = _enum(
    "claim_review_contradiction_status",
    "not_checked",
    "none_found",
    "resolved",
    "unresolved",
)
CLAIM_REVIEW_CONTRADICTION_CHECK = _enum(
    "claim_review_contradiction_check",
    "not_checked",
    "checked_none_found",
    "checked_conflicts_linked",
    "not_applicable",
)
CLAIM_REVIEW_SENSITIVITY = _enum(
    "claim_review_sensitivity",
    "public",
    "controlled",
    "secret_method",
    "restricted",
)
CLAIM_REVIEW_SECRET_EXPOSURE = _enum(
    "claim_review_secret_exposure",
    "none",
    "general_principle",
    "method_detail",
    "operational_secret",
)


IMMUTABLE_TABLES = (
    "source_versions",
    "citation_verification_evidence",
    "source_review_decisions",
    "claim_review_decisions",
    "evidence_card_versions",
)


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", UUID, nullable=False),
        sa.Column("canonical_key", sa.String(length=512), nullable=False),
        sa.Column("submitted_by_user_id", UUID, nullable=False),
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
            "length(trim(canonical_key)) > 0",
            name="ck_sources_canonical_key_present",
        ),
        sa.CheckConstraint(
            "row_version > 0", name="ck_sources_row_version_positive"
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sources"),
        sa.UniqueConstraint("canonical_key", name="uq_sources_canonical_key"),
    )
    op.create_index(
        "ix_sources_submitter_created",
        "sources",
        ["submitted_by_user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "source_versions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("source_id", UUID, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("citation_id", UUID, nullable=False),
        sa.Column(
            "domain_schema_version",
            sa.String(length=80),
            server_default="source-version-0.1",
            nullable=False,
        ),
        sa.Column("citation_metadata", JSONB, nullable=False),
        sa.Column("access_metadata", JSONB, nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("content_access", SOURCE_CONTENT_ACCESS, nullable=False),
        sa.Column("source_type", SOURCE_TYPE, nullable=False),
        sa.Column("knowledge_origin", SOURCE_ORIGIN, nullable=False),
        sa.Column("sensitivity", SOURCE_SENSITIVITY, nullable=False),
        sa.Column("submitted_by_user_id", UUID, nullable=False),
        sa.Column("supersedes_version_id", UUID, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "content_access IN "
            "('search_snippet', 'abstract', 'web_extract', 'full_text', 'pdf_text')",
            name="source_version_content_access",
        ),
        sa.CheckConstraint(
            "source_type IN "
            "('journal_article', 'conference_paper', 'preprint', "
            "'academic_book', 'book_chapter', 'practitioner_book', "
            "'web_article', 'interview', 'transcript', 'archival_material', "
            "'internal_analysis')",
            name="source_version_source_type",
        ),
        sa.CheckConstraint(
            "knowledge_origin IN "
            "('scientific_evidence', 'expert_practice', 'personal_interpretation')",
            name="source_version_knowledge_origin",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('public', 'controlled', 'secret_method', 'restricted')",
            name="source_version_sensitivity",
        ),
        sa.CheckConstraint(
            "version_number > 0", name="ck_source_versions_version_positive"
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name="ck_source_versions_content_hash"
        ),
        sa.CheckConstraint(
            "length(trim(domain_schema_version)) > 0",
            name="ck_source_versions_schema_present",
        ),
        sa.CheckConstraint(
            "supersedes_version_id IS NULL OR supersedes_version_id <> id",
            name="ck_source_versions_no_self_supersession",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_version_id"],
            ["source_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_versions"),
        sa.UniqueConstraint(
            "source_id",
            "version_number",
            name="uq_source_versions_source_version",
        ),
        sa.UniqueConstraint(
            "source_id", "content_hash", name="uq_source_versions_source_hash"
        ),
    )
    op.create_index(
        "ix_source_versions_citation",
        "source_versions",
        ["citation_id"],
        unique=False,
    )
    op.create_index(
        "ix_source_versions_hash", "source_versions", ["content_hash"], unique=False
    )
    op.create_index(
        "ix_source_versions_source_created",
        "source_versions",
        ["source_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_versions_submitter_created",
        "source_versions",
        ["submitted_by_user_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "citation_verification_evidence",
        sa.Column("id", UUID, nullable=False),
        sa.Column("source_version_id", UUID, nullable=False),
        sa.Column("citation_id", UUID, nullable=False),
        sa.Column("verification_scope", CITATION_SCOPE, nullable=False),
        sa.Column("verification_method", CITATION_METHOD, nullable=False),
        sa.Column("resolver_result", JSONB, nullable=False),
        sa.Column("verified_identifier", sa.String(length=1024), nullable=False),
        sa.Column("checked_locator", sa.Text(), nullable=False),
        sa.Column("reviewer_user_id", UUID, nullable=False),
        sa.Column("evidence_checksum", sa.String(length=64), nullable=False),
        sa.Column(
            "domain_schema_version",
            sa.String(length=80),
            server_default="citation-verification-evidence-0.1",
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "verification_method IN "
            "('metadata_resolver', 'canonical_locator', 'full_text_locator', "
            "'manual_review', 'legacy_migration')",
            name="citation_verification_method",
        ),
        sa.CheckConstraint(
            "verification_scope IN ('metadata', 'full_text')",
            name="citation_verification_scope",
        ),
        sa.CheckConstraint(
            "length(evidence_checksum) = 64",
            name="ck_citation_verification_checksum",
        ),
        sa.CheckConstraint(
            "length(trim(domain_schema_version)) > 0",
            name="ck_citation_verification_schema_present",
        ),
        sa.CheckConstraint(
            "length(trim(verified_identifier)) > 0",
            name="ck_citation_verification_identifier_present",
        ),
        sa.CheckConstraint(
            "length(trim(checked_locator)) > 0",
            name="ck_citation_verification_locator_present",
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id"], ["source_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_citation_verification_evidence"),
        sa.UniqueConstraint(
            "source_version_id",
            "evidence_checksum",
            name="uq_citation_verification_source_checksum",
        ),
    )
    op.create_index(
        "ix_citation_verification_source_scope",
        "citation_verification_evidence",
        ["source_version_id", "verification_scope", "verified_at"],
        unique=False,
    )
    op.create_index(
        "ix_citation_verification_citation",
        "citation_verification_evidence",
        ["citation_id"],
        unique=False,
    )
    op.create_index(
        "ix_citation_verification_reviewer",
        "citation_verification_evidence",
        ["reviewer_user_id", "verified_at"],
        unique=False,
    )

    op.create_table(
        "source_review_decisions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("source_version_id", UUID, nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("decision", SOURCE_REVIEW_DECISION, nullable=False),
        sa.Column("resulting_status", SOURCE_REVIEW_STATUS, nullable=False),
        sa.Column("reviewer_user_id", UUID, nullable=False),
        sa.Column("actor_role_snapshot", JSONB, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("citation_verification_evidence_id", UUID, nullable=True),
        sa.Column("claim_eligibility", SOURCE_REVIEW_ELIGIBILITY, nullable=False),
        sa.Column(
            "extraction_permission", SOURCE_REVIEW_EXTRACTION, nullable=False
        ),
        sa.Column("storage_permission", SOURCE_REVIEW_STORAGE, nullable=False),
        sa.Column(
            "sensitive_information_level", SOURCE_REVIEW_SENSITIVITY, nullable=False
        ),
        sa.Column(
            "contradicting_evidence_checked",
            SOURCE_REVIEW_CONTRADICTION,
            nullable=False,
        ),
        sa.Column("extraction_scope", JSONB, nullable=False),
        sa.Column(
            "domain_schema_version",
            sa.String(length=80),
            server_default="source-review-decision-0.1",
            nullable=False,
        ),
        sa.Column("decision_checksum", sa.String(length=64), nullable=False),
        sa.Column("supersedes_decision_id", UUID, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'corrected', 'superseded', 'revoked')",
            name="source_review_decision_kind",
        ),
        sa.CheckConstraint(
            "resulting_status IN ('approved', 'rejected', 'superseded', 'revoked')",
            name="source_review_resulting_status",
        ),
        sa.CheckConstraint(
            "claim_eligibility IN "
            "('not_assessed', 'eligible', 'eligible_with_limits', 'ineligible')",
            name="source_review_claim_eligibility",
        ),
        sa.CheckConstraint(
            "extraction_permission IN "
            "('none', 'metadata_only', 'selected_sections', 'full_text')",
            name="source_review_extraction_permission",
        ),
        sa.CheckConstraint(
            "storage_permission IN "
            "('none', 'derived_knowledge_only', 'derived_with_short_excerpt')",
            name="source_review_storage_permission",
        ),
        sa.CheckConstraint(
            "sensitive_information_level IN "
            "('public', 'controlled', 'secret_method', 'restricted')",
            name="source_review_sensitivity",
        ),
        sa.CheckConstraint(
            "contradicting_evidence_checked IN "
            "('not_checked', 'checked_none_found', 'checked_conflicts_linked', "
            "'not_applicable')",
            name="source_review_contradiction_check",
        ),
        sa.CheckConstraint(
            "sequence_number > 0", name="ck_source_review_sequence_positive"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name="ck_source_review_reason_present"
        ),
        sa.CheckConstraint(
            "length(decision_checksum) = 64",
            name="ck_source_review_decision_checksum",
        ),
        sa.CheckConstraint(
            "supersedes_decision_id IS NULL OR supersedes_decision_id <> id",
            name="ck_source_review_no_self_supersession",
        ),
        sa.CheckConstraint(
            "decision <> 'approved' OR resulting_status = 'approved'",
            name="ck_source_review_approved_result",
        ),
        sa.CheckConstraint(
            "decision <> 'rejected' OR resulting_status = 'rejected'",
            name="ck_source_review_rejected_result",
        ),
        sa.CheckConstraint(
            "decision <> 'superseded' OR resulting_status = 'superseded'",
            name="ck_source_review_superseded_result",
        ),
        sa.CheckConstraint(
            "decision <> 'revoked' OR resulting_status = 'revoked'",
            name="ck_source_review_revoked_result",
        ),
        sa.CheckConstraint(
            "resulting_status <> 'approved' OR "
            "(citation_verification_evidence_id IS NOT NULL AND "
            "claim_eligibility IN ('eligible', 'eligible_with_limits') AND "
            "extraction_permission IN ('selected_sections', 'full_text'))",
            name="ck_source_review_approval_requirements",
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id"], ["source_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["citation_verification_evidence_id"],
            ["citation_verification_evidence.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision_id"],
            ["source_review_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_review_decisions"),
        sa.UniqueConstraint(
            "source_version_id",
            "sequence_number",
            name="uq_source_review_version_sequence",
        ),
        sa.UniqueConstraint(
            "source_version_id",
            "decision_checksum",
            name="uq_source_review_version_checksum",
        ),
    )
    op.create_index(
        "ix_source_review_version_created",
        "source_review_decisions",
        ["source_version_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_review_reviewer_created",
        "source_review_decisions",
        ["reviewer_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_source_review_result_created",
        "source_review_decisions",
        ["resulting_status", "created_at"],
        unique=False,
    )

    op.create_table(
        "claim_candidates",
        sa.Column("id", UUID, nullable=False),
        sa.Column("source_version_id", UUID, nullable=False),
        sa.Column("canonical_claim_id", UUID, nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("claim_role", CLAIM_ROLE, nullable=False),
        sa.Column(
            "claim_polarity", CLAIM_POLARITY, server_default="supports", nullable=False
        ),
        sa.Column("locator", JSONB, nullable=False),
        sa.Column("extraction_provenance", JSONB, nullable=False),
        sa.Column("model_run_metadata", JSONB, nullable=False),
        sa.Column("proposed_evidence_card", JSONB, nullable=False),
        sa.Column(
            "candidate_schema_version",
            sa.String(length=80),
            server_default="claim-candidate-0.1",
            nullable=False,
        ),
        sa.Column("candidate_checksum", sa.String(length=64), nullable=False),
        sa.Column("extraction_confidence", sa.Float(), nullable=False),
        sa.Column("evidence_card_eligible", sa.Boolean(), nullable=False),
        sa.Column("status", CLAIM_STATUS, server_default="draft", nullable=False),
        sa.Column("submitted_by_user_id", UUID, nullable=False),
        sa.Column("created_by_kind", CLAIM_CREATOR, nullable=False),
        sa.Column("supersedes_candidate_id", UUID, nullable=True),
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
            "claim_role IN "
            "('result', 'method', 'background', 'hypothesis', 'discussion', "
            "'expert_opinion', 'context_only')",
            name="claim_candidate_role",
        ),
        sa.CheckConstraint(
            "claim_polarity IN ('supports', 'contradicts', 'qualifies')",
            name="claim_candidate_polarity",
        ),
        sa.CheckConstraint(
            "created_by_kind IN ('human', 'glm', 'pipeline')",
            name="claim_candidate_created_by_kind",
        ),
        sa.CheckConstraint(
            "status IN "
            "('draft', 'submitted', 'under_review', 'approved', 'rejected', "
            "'superseded', 'revoked')",
            name="claim_candidate_workflow_status",
        ),
        sa.CheckConstraint(
            "length(trim(claim_text)) > 0",
            name="ck_claim_candidates_claim_present",
        ),
        sa.CheckConstraint(
            "length(trim(candidate_schema_version)) > 0",
            name="ck_claim_candidates_schema_present",
        ),
        sa.CheckConstraint(
            "length(candidate_checksum) = 64",
            name="ck_claim_candidates_checksum",
        ),
        sa.CheckConstraint(
            "extraction_confidence >= 0.0 AND extraction_confidence <= 1.0",
            name="ck_claim_candidates_extraction_confidence",
        ),
        sa.CheckConstraint(
            "claim_role <> 'context_only' OR evidence_card_eligible = false",
            name="ck_claim_candidates_context_not_evidence_eligible",
        ),
        sa.CheckConstraint(
            "supersedes_candidate_id IS NULL OR supersedes_candidate_id <> id",
            name="ck_claim_candidates_no_self_supersession",
        ),
        sa.CheckConstraint(
            "row_version > 0", name="ck_claim_candidates_row_version_positive"
        ),
        sa.ForeignKeyConstraint(
            ["source_version_id"], ["source_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_candidate_id"],
            ["claim_candidates.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_claim_candidates"),
        sa.UniqueConstraint(
            "source_version_id",
            "candidate_checksum",
            name="uq_claim_candidates_source_checksum",
        ),
    )
    op.create_index(
        "ix_claim_candidates_source_status",
        "claim_candidates",
        ["source_version_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_claim_candidates_status_created",
        "claim_candidates",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_claim_candidates_submitter_status",
        "claim_candidates",
        ["submitted_by_user_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_claim_candidates_canonical_claim",
        "claim_candidates",
        ["canonical_claim_id"],
        unique=False,
    )

    op.create_table(
        "claim_review_decisions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("claim_candidate_id", UUID, nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("decision", CLAIM_REVIEW_DECISION, nullable=False),
        sa.Column("resulting_status", CLAIM_REVIEW_STATUS, nullable=False),
        sa.Column("reviewer_user_id", UUID, nullable=False),
        sa.Column("actor_role_snapshot", JSONB, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("confidence", JSONB, nullable=True),
        sa.Column("limitations", JSONB, nullable=False),
        sa.Column("evidence_class", CLAIM_REVIEW_CLASS, nullable=True),
        sa.Column("knowledge_origin", CLAIM_REVIEW_ORIGIN, nullable=True),
        sa.Column("evidence_level", CLAIM_REVIEW_LEVEL, nullable=True),
        sa.Column("claim_eligibility", CLAIM_REVIEW_ELIGIBILITY, nullable=False),
        sa.Column("storage_permission", CLAIM_REVIEW_STORAGE, nullable=False),
        sa.Column(
            "contradiction_status", CLAIM_REVIEW_CONTRADICTION, nullable=False
        ),
        sa.Column(
            "contradiction_check_status",
            CLAIM_REVIEW_CONTRADICTION_CHECK,
            nullable=False,
        ),
        sa.Column("contradicting_evidence_ids", JSONB, nullable=False),
        sa.Column(
            "sensitive_information_level", CLAIM_REVIEW_SENSITIVITY, nullable=False
        ),
        sa.Column(
            "secret_exposure_level", CLAIM_REVIEW_SECRET_EXPOSURE, nullable=False
        ),
        sa.Column(
            "policy_schema_version",
            sa.String(length=80),
            server_default="claim-review-decision-0.1",
            nullable=False,
        ),
        sa.Column("decision_checksum", sa.String(length=64), nullable=False),
        sa.Column("supersedes_decision_id", UUID, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'corrected', 'superseded', 'revoked')",
            name="claim_review_decision_kind",
        ),
        sa.CheckConstraint(
            "resulting_status IN ('approved', 'rejected', 'superseded', 'revoked')",
            name="claim_review_resulting_status",
        ),
        sa.CheckConstraint(
            "evidence_class IS NULL OR evidence_class IN "
            "('controlled_experiment', 'quasi_experiment', 'observational_study', "
            "'systematic_review', 'meta_analysis', 'narrative_review', "
            "'expert_instruction', 'expert_case_analysis', 'practitioner_report', "
            "'historical_primary_record', 'historical_secondary_analysis', "
            "'analyst_interpretation', 'anecdotal_observation')",
            name="claim_review_evidence_class",
        ),
        sa.CheckConstraint(
            "knowledge_origin IS NULL OR knowledge_origin IN "
            "('scientific_evidence', 'expert_practice', 'personal_interpretation')",
            name="claim_review_knowledge_origin",
        ),
        sa.CheckConstraint(
            "evidence_level IS NULL OR evidence_level IN "
            "('empirical', 'review', 'practitioner', 'anecdotal')",
            name="claim_review_evidence_level",
        ),
        sa.CheckConstraint(
            "claim_eligibility IN "
            "('not_assessed', 'eligible', 'eligible_with_limits', 'ineligible')",
            name="claim_review_claim_eligibility",
        ),
        sa.CheckConstraint(
            "storage_permission IN "
            "('none', 'derived_knowledge_only', 'derived_with_short_excerpt')",
            name="claim_review_storage_permission",
        ),
        sa.CheckConstraint(
            "contradiction_status IN "
            "('not_checked', 'none_found', 'resolved', 'unresolved')",
            name="claim_review_contradiction_status",
        ),
        sa.CheckConstraint(
            "contradiction_check_status IN "
            "('not_checked', 'checked_none_found', 'checked_conflicts_linked', "
            "'not_applicable')",
            name="claim_review_contradiction_check",
        ),
        sa.CheckConstraint(
            "sensitive_information_level IN "
            "('public', 'controlled', 'secret_method', 'restricted')",
            name="claim_review_sensitivity",
        ),
        sa.CheckConstraint(
            "secret_exposure_level IN "
            "('none', 'general_principle', 'method_detail', 'operational_secret')",
            name="claim_review_secret_exposure",
        ),
        sa.CheckConstraint(
            "sequence_number > 0", name="ck_claim_review_sequence_positive"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name="ck_claim_review_reason_present"
        ),
        sa.CheckConstraint(
            "length(decision_checksum) = 64",
            name="ck_claim_review_decision_checksum",
        ),
        sa.CheckConstraint(
            "supersedes_decision_id IS NULL OR supersedes_decision_id <> id",
            name="ck_claim_review_no_self_supersession",
        ),
        sa.CheckConstraint(
            "decision <> 'approved' OR resulting_status = 'approved'",
            name="ck_claim_review_approved_result",
        ),
        sa.CheckConstraint(
            "decision <> 'rejected' OR resulting_status = 'rejected'",
            name="ck_claim_review_rejected_result",
        ),
        sa.CheckConstraint(
            "decision <> 'superseded' OR resulting_status = 'superseded'",
            name="ck_claim_review_superseded_result",
        ),
        sa.CheckConstraint(
            "decision <> 'revoked' OR resulting_status = 'revoked'",
            name="ck_claim_review_revoked_result",
        ),
        sa.CheckConstraint(
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
        sa.ForeignKeyConstraint(
            ["claim_candidate_id"], ["claim_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision_id"],
            ["claim_review_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_claim_review_decisions"),
        sa.UniqueConstraint(
            "claim_candidate_id",
            "sequence_number",
            name="uq_claim_review_candidate_sequence",
        ),
        sa.UniqueConstraint(
            "claim_candidate_id",
            "decision_checksum",
            name="uq_claim_review_candidate_checksum",
        ),
    )
    op.create_index(
        "ix_claim_review_candidate_created",
        "claim_review_decisions",
        ["claim_candidate_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_claim_review_reviewer_created",
        "claim_review_decisions",
        ["reviewer_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_claim_review_result_created",
        "claim_review_decisions",
        ["resulting_status", "created_at"],
        unique=False,
    )

    op.create_table(
        "evidence_card_versions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("evidence_card_id", UUID, nullable=False),
        sa.Column("domain_object_id", UUID, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("claim_candidate_id", UUID, nullable=False),
        sa.Column("claim_review_decision_id", UUID, nullable=False),
        sa.Column("source_id", UUID, nullable=False),
        sa.Column("source_version_id", UUID, nullable=False),
        sa.Column("citation_id", UUID, nullable=False),
        sa.Column("canonical_claim_id", UUID, nullable=False),
        sa.Column(
            "domain_schema_version",
            sa.String(length=80),
            server_default="evidence-0.2",
            nullable=False,
        ),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("payload_checksum", sa.String(length=64), nullable=False),
        sa.Column("supersedes_version_id", UUID, nullable=True),
        sa.Column("created_by_user_id", UUID, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version_number > 0", name="ck_evidence_card_versions_version_positive"
        ),
        sa.CheckConstraint(
            "length(trim(domain_schema_version)) > 0",
            name="ck_evidence_card_versions_schema_present",
        ),
        sa.CheckConstraint(
            "length(payload_checksum) = 64",
            name="ck_evidence_card_versions_checksum",
        ),
        sa.CheckConstraint(
            "supersedes_version_id IS NULL OR supersedes_version_id <> id",
            name="ck_evidence_card_versions_no_self_supersession",
        ),
        sa.ForeignKeyConstraint(
            ["claim_candidate_id"], ["claim_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["claim_review_decision_id"],
            ["claim_review_decisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["source_version_id"], ["source_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_version_id"],
            ["evidence_card_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_evidence_card_versions"),
        sa.UniqueConstraint(
            "evidence_card_id",
            "version_number",
            name="uq_evidence_card_versions_family_version",
        ),
        sa.UniqueConstraint(
            "domain_object_id", name="uq_evidence_card_versions_domain_object"
        ),
        sa.UniqueConstraint(
            "claim_review_decision_id",
            name="uq_evidence_card_versions_review_decision",
        ),
    )
    op.create_index(
        "ix_evidence_card_versions_claim_created",
        "evidence_card_versions",
        ["claim_candidate_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_card_versions_source_version",
        "evidence_card_versions",
        ["source_version_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_card_versions_citation",
        "evidence_card_versions",
        ["citation_id"],
        unique=False,
    )
    op.create_index(
        "ix_evidence_card_versions_canonical_claim",
        "evidence_card_versions",
        ["canonical_claim_id"],
        unique=False,
    )

    op.execute(
        """
        CREATE FUNCTION magicforge_reject_governance_record_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $magicforge$
        BEGIN
            RAISE EXCEPTION '% is immutable', TG_TABLE_NAME
                USING ERRCODE = '55000';
        END;
        $magicforge$
        """
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
        CREATE FUNCTION magicforge_guard_workflow_envelope()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $magicforge$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF TG_TABLE_NAME = 'claim_candidates' AND
                   NEW.status NOT IN ('draft', 'submitted') THEN
                    RAISE EXCEPTION
                        'claim candidate initial status must be draft or submitted'
                        USING ERRCODE = '55000';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION '% records cannot be deleted', TG_TABLE_NAME
                    USING ERRCODE = '55000';
            END IF;
            IF TG_TABLE_NAME = 'sources' AND
               (to_jsonb(NEW) - 'updated_at' - 'row_version') IS DISTINCT FROM
               (to_jsonb(OLD) - 'updated_at' - 'row_version') THEN
                RAISE EXCEPTION 'sources identity fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF TG_TABLE_NAME = 'claim_candidates' AND
               (to_jsonb(NEW) - 'status' - 'updated_at' - 'row_version')
                   IS DISTINCT FROM
               (to_jsonb(OLD) - 'status' - 'updated_at' - 'row_version') THEN
                RAISE EXCEPTION 'claim candidate proposal fields are immutable'
                    USING ERRCODE = '55000';
            END IF;
            IF TG_TABLE_NAME = 'claim_candidates' AND
               NEW.status IS DISTINCT FROM OLD.status AND
               (
                   SELECT resulting_status
                   FROM claim_review_decisions
                   WHERE claim_candidate_id = OLD.id
                   ORDER BY sequence_number DESC
                   LIMIT 1
               ) IS DISTINCT FROM NEW.status THEN
                RAISE EXCEPTION
                    'claim candidate status must match the latest review decision'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $magicforge$
        """
    )
    for table_name in ("sources", "claim_candidates"):
        operations = (
            "INSERT OR UPDATE OR DELETE"
            if table_name == "claim_candidates"
            else "UPDATE OR DELETE"
        )
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_workflow_guard
            BEFORE {operations} ON {table_name}
            FOR EACH ROW
            EXECUTE FUNCTION magicforge_guard_workflow_envelope()
            """
        )


def downgrade() -> None:
    for table_name in ("claim_candidates", "sources"):
        op.execute(f"DROP TRIGGER {table_name}_workflow_guard ON {table_name}")
    op.execute("DROP FUNCTION magicforge_guard_workflow_envelope()")
    for table_name in reversed(IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER {table_name}_immutable ON {table_name}")
    op.execute("DROP FUNCTION magicforge_reject_governance_record_mutation()")

    op.drop_index(
        "ix_evidence_card_versions_canonical_claim",
        table_name="evidence_card_versions",
    )
    op.drop_index(
        "ix_evidence_card_versions_citation", table_name="evidence_card_versions"
    )
    op.drop_index(
        "ix_evidence_card_versions_source_version",
        table_name="evidence_card_versions",
    )
    op.drop_index(
        "ix_evidence_card_versions_claim_created",
        table_name="evidence_card_versions",
    )
    op.drop_table("evidence_card_versions")

    op.drop_index("ix_claim_review_result_created", table_name="claim_review_decisions")
    op.drop_index(
        "ix_claim_review_reviewer_created", table_name="claim_review_decisions"
    )
    op.drop_index(
        "ix_claim_review_candidate_created", table_name="claim_review_decisions"
    )
    op.drop_table("claim_review_decisions")

    op.drop_index(
        "ix_claim_candidates_canonical_claim", table_name="claim_candidates"
    )
    op.drop_index(
        "ix_claim_candidates_submitter_status", table_name="claim_candidates"
    )
    op.drop_index("ix_claim_candidates_status_created", table_name="claim_candidates")
    op.drop_index("ix_claim_candidates_source_status", table_name="claim_candidates")
    op.drop_table("claim_candidates")

    op.drop_index(
        "ix_source_review_result_created", table_name="source_review_decisions"
    )
    op.drop_index(
        "ix_source_review_reviewer_created", table_name="source_review_decisions"
    )
    op.drop_index(
        "ix_source_review_version_created", table_name="source_review_decisions"
    )
    op.drop_table("source_review_decisions")

    op.drop_index(
        "ix_citation_verification_reviewer",
        table_name="citation_verification_evidence",
    )
    op.drop_index(
        "ix_citation_verification_citation",
        table_name="citation_verification_evidence",
    )
    op.drop_index(
        "ix_citation_verification_source_scope",
        table_name="citation_verification_evidence",
    )
    op.drop_table("citation_verification_evidence")

    op.drop_index(
        "ix_source_versions_submitter_created", table_name="source_versions"
    )
    op.drop_index("ix_source_versions_source_created", table_name="source_versions")
    op.drop_index("ix_source_versions_hash", table_name="source_versions")
    op.drop_index("ix_source_versions_citation", table_name="source_versions")
    op.drop_table("source_versions")

    op.drop_index("ix_sources_submitter_created", table_name="sources")
    op.drop_table("sources")
