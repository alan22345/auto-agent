"""Add ARCHITECT_DESIGNING / AWAITING_DESIGN_APPROVAL / ARCHITECT_BACKLOG_EMIT — ADR-015 §2 / Phase 6.

Revision ID: 037
Revises: 036
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Mirror 032 / 035 / 036: add both the uppercase NAME form (SQLAlchemy's
    # default) and the lowercase VALUE form so raw-SQL paths still match.
    # Idempotent — `IF NOT EXISTS` makes repeated upgrades safe.
    for name in (
        "ARCHITECT_DESIGNING",
        "architect_designing",
        "AWAITING_DESIGN_APPROVAL",
        "awaiting_design_approval",
        "ARCHITECT_BACKLOG_EMIT",
        "architect_backlog_emit",
    ):
        op.execute(f"ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS '{name}'")


def downgrade() -> None:
    # Postgres < 12 cannot drop enum values; leave them in place — same
    # posture as 032 / 035 / 036.
    pass
