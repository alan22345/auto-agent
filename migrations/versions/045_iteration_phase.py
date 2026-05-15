"""ADR-017 — add ITERATING taskstatus + ARCHITECT_ITERATING triophase.

Both are idempotent ``ADD VALUE IF NOT EXISTS`` so the migration is safe
to re-run on stacks that already applied it manually.

Revision ID: 045
Revises: 044
Create Date: 2026-05-15
"""

from __future__ import annotations

from alembic import op

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'ITERATING'")
    op.execute("ALTER TYPE triophase ADD VALUE IF NOT EXISTS 'ARCHITECT_ITERATING'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE DROP VALUE. Downgrade is a no-op — enum values
    # left in place don't affect anything that hasn't been written to use them.
    pass
