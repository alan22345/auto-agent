"""ADR-018 — add SCAFFOLD complexity + 8 scaffold-flow taskstatus values.

All ``ADD VALUE IF NOT EXISTS`` so the migration is safe to re-run on
stacks that already applied it manually.

Revision ID: 046
Revises: 045
Create Date: 2026-05-18
"""

from __future__ import annotations

from alembic import op

revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New TaskComplexity value.
    op.execute("ALTER TYPE taskcomplexity ADD VALUE IF NOT EXISTS 'scaffold'")

    # ADR-018 scaffold parent state machine — 8 new TaskStatus values.
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'awaiting_intent_grill'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'building_root_adr'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'awaiting_root_adr_approval'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'building_domain_adrs'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'awaiting_domain_adr_approval'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'dispatching_domain_builds'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'building_domains'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'awaiting_final_verification'")
    # ADR-018 Stage 8 — per-domain grill round runs before each domain
    # ADR is written. When the grill agent pauses on a question the
    # parent parks in this status until the user (or PO standin) answers.
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'awaiting_domain_grill'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE DROP VALUE. Downgrade is a no-op — enum values
    # left in place don't affect anything that hasn't been written to use them.
    pass
