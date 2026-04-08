"""Add harness onboarding columns to repos table.

Revision ID: 007
Revises: 006
Create Date: 2026-04-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("repos", sa.Column("harness_onboarded", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("repos", sa.Column("harness_pr_url", sa.String(512), nullable=True))


def downgrade() -> None:
    op.drop_column("repos", "harness_pr_url")
    op.drop_column("repos", "harness_onboarded")
