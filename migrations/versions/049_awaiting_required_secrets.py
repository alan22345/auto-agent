"""ADR-019 T7 — add awaiting_required_secrets to the taskstatus enum.

Without this migration any attempt to write TaskStatus.AWAITING_REQUIRED_SECRETS
to the Postgres ``taskstatus`` enum column fails with an enum constraint error.

``ADD VALUE IF NOT EXISTS`` keeps the migration safe to re-run on stacks that
already applied the value manually.

Revision ID: 049
Revises: 048
Create Date: 2026-05-19
"""

from __future__ import annotations

from alembic import op

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ADR-019 T7 — intermediate gate status for scaffold parents waiting on
    # architect-required secrets to be populated before Phase D can run.
    op.execute(
        "ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'awaiting_required_secrets'"
    )


def downgrade() -> None:
    # Postgres has no ALTER TYPE DROP VALUE. Downgrade is a no-op — enum values
    # left in place don't affect anything that hasn't been written to use them.
    pass
