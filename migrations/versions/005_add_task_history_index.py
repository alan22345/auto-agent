"""Add index on task_history.task_id for query performance.

Revision ID: 005
Revises: 004
Create Date: 2026-04-06
"""
from typing import Sequence, Union

from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_task_history_task_id", "task_history", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_history_task_id", table_name="task_history")
