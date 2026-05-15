"""Add ON DELETE rules to tasks.id child FKs so ``DELETE FROM tasks`` works.

The legacy ``DELETE /tasks/{id}`` endpoint only cleared ``task_history``
before deleting the task, but five other child tables have FKs to
``tasks.id`` with no ON DELETE rule (defaults to NO ACTION → integrity
error). Tasks that reached states writing to ``architect_attempts``,
``gate_decisions``, ``task_outcomes`` etc. became undeletable.

Five FKs get rewritten here. The remaining six (architect_attempts,
review_attempts, task_messages, trio_review_attempts, verify_attempts,
usage_events) already have ON DELETE rules in the live schema and aren't
touched.

Rules:
  CASCADE   — per-task data with no value after the task is gone
              (task_history, task_outcomes, gate_decisions).
  SET NULL  — records that pre-exist or outlive the task
              (suggestions.task_id, tasks.parent_task_id — orphan
              sub-tasks rather than recursive-nuke them).

Idempotent: each FK is dropped IF EXISTS then re-added with the new rule.

Revision ID: 044
Revises: 043
Create Date: 2026-05-15
"""

from __future__ import annotations

from alembic import op

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None


# (table, constraint_name, column, ondelete_clause)
_CASCADE_FKS = [
    ("task_history", "task_history_task_id_fkey", "task_id", "ON DELETE CASCADE"),
    ("task_outcomes", "task_outcomes_task_id_fkey", "task_id", "ON DELETE CASCADE"),
    ("gate_decisions", "gate_decisions_task_id_fkey", "task_id", "ON DELETE CASCADE"),
    ("suggestions", "suggestions_task_id_fkey", "task_id", "ON DELETE SET NULL"),
    ("tasks", "fk_tasks_parent_task_id", "parent_task_id", "ON DELETE SET NULL"),
]


def upgrade() -> None:
    for table, name, column, rule in _CASCADE_FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(
            f"ALTER TABLE {table} "
            f"ADD CONSTRAINT {name} FOREIGN KEY ({column}) "
            f"REFERENCES tasks(id) {rule}"
        )


def downgrade() -> None:
    for table, name, column, _ in _CASCADE_FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY ({column}) REFERENCES tasks(id)"
        )
