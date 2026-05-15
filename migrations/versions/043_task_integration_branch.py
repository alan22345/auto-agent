"""Add ``Task.integration_branch`` — ADR-015 Phase 7.7.

The trio integration branch was previously ``trio/<task_id>`` (derived
inline at every call site). Phase 7.7 renames the new shape to
``auto-agent/<slug>-<task_id>`` and stores it on the task so:

  * every call site reads one source of truth (no slug recomputation);
  * in-flight tasks created before this rename keep their legacy
    ``trio/<id>`` branch — they have a NULL value here and the resolver
    falls back accordingly.

Nullable on purpose. Idempotent so it can re-run safely on stacks that
already applied it manually.

Revision ID: 043
Revises: 042
Create Date: 2026-05-15
"""

from __future__ import annotations

from alembic import op

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS integration_branch VARCHAR(255)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS integration_branch")
