"""Add FINAL_REVIEW / ARCHITECT_GAP_FIX — ADR-015 §4 / Phase 7.

Revision ID: 038
Revises: 037
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Mirror 032 / 035 / 036 / 037: add both the uppercase NAME form
    # (SQLAlchemy's default) and the lowercase VALUE form so raw-SQL paths
    # still match. Idempotent — `IF NOT EXISTS` makes repeated upgrades safe.
    for name in (
        "FINAL_REVIEW",
        "final_review",
        "ARCHITECT_GAP_FIX",
        "architect_gap_fix",
    ):
        op.execute(f"ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS '{name}'")


def downgrade() -> None:
    # Postgres < 12 cannot drop enum values; leave them in place — same
    # posture as 032 / 035 / 036 / 037.
    pass
