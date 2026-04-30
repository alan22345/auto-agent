"""Add search_sessions and search_messages tables.

Revision ID: 019
Revises: 018
Create Date: 2026-04-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS search_sessions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title VARCHAR(512) NOT NULL DEFAULT 'New search',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_search_sessions_user_id ON search_sessions(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_search_sessions_updated_at ON search_sessions(updated_at DESC)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS search_messages (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES search_sessions(id) ON DELETE CASCADE,
            role VARCHAR(16) NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            tool_events JSONB NOT NULL DEFAULT '[]'::jsonb,
            truncated BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_search_messages_session_id ON search_messages(session_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS search_messages")
    op.execute("DROP TABLE IF EXISTS search_sessions")
