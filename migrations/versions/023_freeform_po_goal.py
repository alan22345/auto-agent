"""Add po_goal to freeform_configs.

Revision ID: 023
Revises: 022
Create Date: 2026-05-08
"""
from typing import Sequence, Union

from alembic import op


revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE freeform_configs "
        "ADD COLUMN IF NOT EXISTS po_goal TEXT NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE freeform_configs DROP COLUMN IF EXISTS po_goal")
