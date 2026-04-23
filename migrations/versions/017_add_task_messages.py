"""Add task_messages table for in-task user feedback.

Users can post messages to a task while it's running. The agent reads
unread messages between turns and injects them as user input.

Revision ID: 017
Revises: 016
Create Date: 2026-04-23
"""
from typing import Sequence, Union

from alembic import op

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS task_messages (
            id SERIAL PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            sender VARCHAR(128) NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            read_by_agent_at TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_task_messages_task_id ON task_messages(task_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_task_messages_unread "
        "ON task_messages(task_id) WHERE read_by_agent_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS task_messages")
