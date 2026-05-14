"""Add AWAITING_PLAN_APPROVAL + ADDRESSING_COMMENTS task statuses — ADR-015 §5 Phase 5.

Revision ID: 036
Revises: 035
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Same convention as 032/035: add both the uppercase NAME form (SQLAlchemy
    # Enum's default) and the lowercase VALUE form (raw-SQL paths). Idempotent.
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'AWAITING_PLAN_APPROVAL'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'awaiting_plan_approval'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'ADDRESSING_COMMENTS'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'addressing_comments'")


def downgrade() -> None:
    # Postgres < 12 cannot drop enum values; leave them in place — same posture
    # as 032 / 035.
    pass
