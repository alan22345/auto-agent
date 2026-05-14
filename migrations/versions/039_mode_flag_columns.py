"""Add Repo.mode + Task.mode_override columns — ADR-015 §7 Phase 10.

Per-repo default mode plus a bidirectional per-task override. Both
columns hold one of ``"freeform" | "human_in_loop"`` (Python-side
domain); ``Task.mode_override`` is nullable to mean "inherit from
repo".

Defaults are conservative — existing repos get ``"human_in_loop"`` so
the human stays in the loop until the operator explicitly flips a repo
or task to freeform.

Revision ID: 039
Revises: 038
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent — re-running the migration must not error.
    op.execute(
        "ALTER TABLE repos ADD COLUMN IF NOT EXISTS mode VARCHAR(32) "
        "NOT NULL DEFAULT 'human_in_loop'"
    )
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS mode_override VARCHAR(32)")


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS mode_override")
    op.execute("ALTER TABLE repos DROP COLUMN IF EXISTS mode")
