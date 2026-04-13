"""Add subtask tracking fields to tasks.

Revision ID: 012
Revises: 011
Create Date: 2026-04-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS subtasks JSONB")
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS current_subtask INTEGER")
    # Add complex_large to the complexity enum (DB uses uppercase enum names)
    op.execute("ALTER TYPE taskcomplexity ADD VALUE IF NOT EXISTS 'COMPLEX_LARGE'")


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS subtasks")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS current_subtask")
