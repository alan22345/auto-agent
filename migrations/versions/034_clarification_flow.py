"""clarification flow: Repo.product_brief + ArchitectAttempt clarification cols

Revision ID: 034
Revises: 033
Create Date: 2026-05-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "repos",
        sa.Column("product_brief", sa.Text(), nullable=True),
    )
    op.add_column(
        "architect_attempts",
        sa.Column("clarification_question", sa.Text(), nullable=True),
    )
    op.add_column(
        "architect_attempts",
        sa.Column("clarification_answer", sa.Text(), nullable=True),
    )
    op.add_column(
        "architect_attempts",
        sa.Column("clarification_source", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "architect_attempts",
        sa.Column("session_blob_path", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("architect_attempts", "session_blob_path")
    op.drop_column("architect_attempts", "clarification_source")
    op.drop_column("architect_attempts", "clarification_answer")
    op.drop_column("architect_attempts", "clarification_question")
    op.drop_column("repos", "product_brief")
