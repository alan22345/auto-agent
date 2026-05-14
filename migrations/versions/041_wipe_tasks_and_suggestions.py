"""Wipe all tasks + suggestions for the ADR-015 redesign cutover.

Per ADR-015 §15 (narrowed 2026-05-14): drop every ``Task`` and
``Suggestion`` row at deploy time. Per-task children
(``ArchitectAttempt``, ``TrioReviewAttempt``, ``VerifyAttempt``,
``TaskHistory``, ``GateDecision``, etc.) cascade with the parent via
``TRUNCATE ... CASCADE``. ``Repo``, ``User``, ``Session``, smoke
configs, mode flags, and other non-task data are preserved so the
system is immediately usable post-deploy.

``TRUNCATE ... CASCADE`` is idempotent — re-running against an empty
table is a no-op. The migration is therefore safe to live at HEAD;
fresh environments running ``alembic upgrade head`` get an empty
tasks/suggestions table (the desired state), production environments
running it once get the wipe.

Revision ID: 041
Revises: 040
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CASCADE follows every inbound FK; all per-task children
    # (verify_attempts, review_attempts, task_history, gate_decisions,
    # architect_attempts, trio_review_attempts, etc.) are cleared.
    op.execute("TRUNCATE TABLE tasks RESTART IDENTITY CASCADE")
    op.execute("TRUNCATE TABLE suggestions RESTART IDENTITY CASCADE")


def downgrade() -> None:
    # No-op: a wipe has no inverse. Restoring rows would require a
    # backup; ADR-015 §15 explicitly accepts this as a cost of the
    # cutover.
    pass
