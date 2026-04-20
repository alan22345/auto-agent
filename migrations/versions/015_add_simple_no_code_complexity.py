"""Add SIMPLE_NO_CODE to task complexity enum.

For query/research tasks that don't need a repo or coding tools — just
an LLM response (e.g. "sort these by price", "explain this concept").

Revision ID: 015
Revises: 014
Create Date: 2026-04-17
"""
from typing import Sequence, Union

from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE taskcomplexity ADD VALUE IF NOT EXISTS 'SIMPLE_NO_CODE'")


def downgrade() -> None:
    pass  # Can't remove enum values in Postgres
