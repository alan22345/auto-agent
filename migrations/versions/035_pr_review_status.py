"""Add PR_REVIEW task status — ADR-015 §5 Phase 4 (self-PR-review gate).

Revision ID: 035
Revises: 034
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Match migration 032's convention: add both the uppercase NAME form (which
    # SQLAlchemy's Enum(TaskStatus) serializes by default) and the lowercase
    # VALUE form (used by historical raw-SQL paths). Each is idempotent.
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'PR_REVIEW'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'pr_review'")


def downgrade() -> None:
    # Postgres < 12 cannot drop enum values; we leave them in place. The
    # column would have to be dropped and re-typed, which isn't worth the
    # complexity for an additive migration of an in-experiment system.
    pass
