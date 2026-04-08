"""Add awaiting_clarification to TaskStatus enum.

Revision ID: 003
Revises: 002
Create Date: 2026-04-04
"""
from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'awaiting_clarification' AFTER 'awaiting_approval'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; this is a no-op.
    pass
