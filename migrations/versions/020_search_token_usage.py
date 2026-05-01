"""Track token usage per search_messages row.

Revision ID: 020
Revises: 019
Create Date: 2026-05-01
"""
from typing import Sequence, Union

from alembic import op

revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE search_messages "
        "ADD COLUMN IF NOT EXISTS input_tokens INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE search_messages "
        "ADD COLUMN IF NOT EXISTS output_tokens INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE search_messages DROP COLUMN IF EXISTS output_tokens")
    op.execute("ALTER TABLE search_messages DROP COLUMN IF EXISTS input_tokens")
