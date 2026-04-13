"""Add auto_approve_suggestions flag to freeform_configs.

Revision ID: 009
Revises: 008
Create Date: 2026-04-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE freeform_configs "
        "ADD COLUMN IF NOT EXISTS auto_approve_suggestions BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE freeform_configs DROP COLUMN IF EXISTS auto_approve_suggestions")
