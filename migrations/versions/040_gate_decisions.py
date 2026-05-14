"""Add gate_decisions table — ADR-015 §6 Phase 12.

Persists every gate decision (user or freeform standin) so the
web-next gate-history audit panel can reconstruct who decided what at
every gate without scraping the Redis ``standin.decision`` stream.

Revision ID: 040
Revises: 039
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent — re-running the migration must not error.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS gate_decisions (
            id SERIAL PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES tasks(id),
            gate VARCHAR(64) NOT NULL,
            source VARCHAR(64) NOT NULL,
            agent_id VARCHAR(128),
            verdict TEXT NOT NULL DEFAULT '',
            comments TEXT NOT NULL DEFAULT '',
            cited_context JSONB NOT NULL DEFAULT '[]'::jsonb,
            fallback_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_gate_decisions_task_id ON gate_decisions(task_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_gate_decisions_task_id")
    op.execute("DROP TABLE IF EXISTS gate_decisions")
