"""Add auto_start_tasks flag to freeform_configs.

Revision ID: 011
Revises: 010
Create Date: 2026-04-13
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE freeform_configs "
        "ADD COLUMN IF NOT EXISTS auto_start_tasks BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE freeform_configs DROP COLUMN IF EXISTS auto_start_tasks")
