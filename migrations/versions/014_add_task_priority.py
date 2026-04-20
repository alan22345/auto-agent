"""Add priority column to tasks for queue ordering.

Lower number = higher priority. Default 100 (normal). Users can set
priority=0 to jump to the front of the queue.

Revision ID: 014
Revises: 013
Create Date: 2026-04-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "014"
down_revision: Union[str, None] = "013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 100")


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS priority")
