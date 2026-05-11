"""Add per-user Claude auth columns and the BLOCKED_ON_AUTH task status.

Revision ID: 022
Revises: 021
Create Date: 2026-05-05
"""
from typing import Sequence, Union

from alembic import op


revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS claude_auth_status VARCHAR(32) "
        "NOT NULL DEFAULT 'never_paired'"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD COLUMN IF NOT EXISTS claude_paired_at TIMESTAMPTZ NULL"
    )
    op.execute(
        "ALTER TABLE users "
        "DROP CONSTRAINT IF EXISTS users_claude_auth_status_check"
    )
    op.execute(
        "ALTER TABLE users "
        "ADD CONSTRAINT users_claude_auth_status_check "
        "CHECK (claude_auth_status IN ('paired', 'expired', 'never_paired'))"
    )
    # Extend the taskstatus enum used by tasks.status
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'blocked_on_auth'")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_claude_auth_status_check"
    )
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS claude_paired_at")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS claude_auth_status")
    # Note: removing an enum value in Postgres requires recreating the type.
    # Safe to leave 'blocked_on_auth' on the enum during downgrade — no rows
    # reference it once the orchestrator code is rolled back.
