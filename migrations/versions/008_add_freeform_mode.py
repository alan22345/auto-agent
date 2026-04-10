"""Add freeform mode: suggestions, freeform_configs, and task.freeform_mode.

Revision ID: 008
Revises: 007
Create Date: 2026-04-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add freeform_mode to tasks
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS freeform_mode BOOLEAN NOT NULL DEFAULT false")

    # Add FREEFORM to tasksource enum
    op.execute("ALTER TYPE tasksource ADD VALUE IF NOT EXISTS 'freeform'")

    # Create suggestionstatus enum via raw SQL (IF NOT EXISTS handles idempotency)
    op.execute("DO $$ BEGIN CREATE TYPE suggestionstatus AS ENUM ('pending', 'approved', 'rejected'); EXCEPTION WHEN duplicate_object THEN NULL; END $$")

    # Create suggestions table
    op.execute("""
        CREATE TABLE IF NOT EXISTS suggestions (
            id SERIAL PRIMARY KEY,
            repo_id INTEGER NOT NULL REFERENCES repos(id),
            title VARCHAR(512) NOT NULL,
            description TEXT DEFAULT '',
            rationale TEXT DEFAULT '',
            category VARCHAR(100) DEFAULT '',
            priority INTEGER DEFAULT 3,
            status suggestionstatus DEFAULT 'pending',
            task_id INTEGER REFERENCES tasks(id),
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)

    # Create freeform_configs table
    op.execute("""
        CREATE TABLE IF NOT EXISTS freeform_configs (
            id SERIAL PRIMARY KEY,
            repo_id INTEGER NOT NULL UNIQUE REFERENCES repos(id),
            enabled BOOLEAN DEFAULT false,
            dev_branch VARCHAR(128) DEFAULT 'dev',
            analysis_cron VARCHAR(100) DEFAULT '0 9 * * 1',
            last_analysis_at TIMESTAMPTZ,
            ux_knowledge TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS freeform_configs")
    op.execute("DROP TABLE IF EXISTS suggestions")
    op.execute("DROP TYPE IF EXISTS suggestionstatus")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS freeform_mode")
