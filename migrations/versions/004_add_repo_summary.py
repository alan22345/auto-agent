"""Add summary and summary_updated_at to repos table.

Revision ID: 004
Revises: 003
Create Date: 2026-04-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("repos", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("repos", sa.Column("summary_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("repos", "summary_updated_at")
    op.drop_column("repos", "summary")
