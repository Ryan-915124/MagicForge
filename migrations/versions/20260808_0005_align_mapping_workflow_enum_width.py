"""Align Mapping workflow VARCHAR widths with ORM enum metadata.

Revision ID: 20260808_0005
Revises: 20260808_0004
Create Date: 2026-08-08

The Mapping workflow deliberately permits only submitted/terminal states via
table CHECK constraints.  Its ORM columns nevertheless use the shared
``GovernanceWorkflowStatus`` non-native enum, whose longest value is
``under_review`` (12 characters).  SQLAlchemy therefore models those columns
as VARCHAR(12), while the original migration inferred VARCHAR(10) from the
narrower value lists.

Widening the storage type removes that metadata drift without changing the
allowed Mapping state machine.  The existing CHECK constraints remain the
authority for legal values.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260808_0005"
down_revision: str | None = "20260808_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "mapping_proposals",
        "status",
        existing_type=sa.String(length=10),
        type_=sa.String(length=12),
        existing_nullable=False,
    )
    op.alter_column(
        "mapping_review_decisions",
        "resulting_status",
        existing_type=sa.String(length=10),
        type_=sa.String(length=12),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Both table CHECK constraints restrict values to at most 10 characters,
    # so narrowing is lossless for every valid persisted row.
    op.alter_column(
        "mapping_review_decisions",
        "resulting_status",
        existing_type=sa.String(length=12),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
    op.alter_column(
        "mapping_proposals",
        "status",
        existing_type=sa.String(length=12),
        type_=sa.String(length=10),
        existing_nullable=False,
    )
