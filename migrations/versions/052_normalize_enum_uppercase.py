"""Normalize enum case — add uppercase variants of every early Postgres enum value.

Background. Migration 001 (and a few that followed) created the ``taskstatus``,
``taskcomplexity``, ``tasksource``, and ``suggestionstatus`` enums with lower-
case values. SQLAlchemy's :class:`Enum` column type serialises Python enum
*names* (UPPERCASE) — not values — when binding parameters. The mismatch has
been latent for a long time on the VM because someone manually
``ALTER TYPE … RENAME VALUE`` 'd the early values to uppercase there, so the
two halves of the stack happened to agree in production. That manual change
was never captured in alembic, so a fresh ``alembic upgrade head`` (e.g. on a
clean local docker volume) ends up with an enum the application can't
actually query.

This migration converges both worlds by ``ADD VALUE IF NOT EXISTS`` for every
uppercase variant. On the VM it's a no-op (those values are already there).
On a fresh DB it adds them and queries start working. Lowercase values are
left in place — Postgres has no portable ``DROP VALUE`` and no rows reference
them on a fresh DB anyway.

Revision ID: 052
Revises: 051
Create Date: 2026-05-21
"""

from __future__ import annotations

from alembic import op

revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None


# (enum_name, [UPPERCASE_VALUE, ...]) — only the values that migration 001
# (and a handful of follow-ups) created in lowercase. Newer enum values were
# added uppercase by their respective migrations and need no normalisation.
_TO_ADD: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "taskstatus",
        (
            "INTAKE",
            "CLASSIFYING",
            "QUEUED",
            "PLANNING",
            "AWAITING_APPROVAL",
            "AWAITING_CLARIFICATION",
            "CODING",
            "PR_CREATED",
            "AWAITING_CI",
            "AWAITING_REVIEW",
            "DONE",
            "BLOCKED",
            "FAILED",
            "BLOCKED_ON_AUTH",
        ),
    ),
    ("taskcomplexity", ("SIMPLE", "COMPLEX")),
    ("tasksource", ("SLACK", "LINEAR", "TELEGRAM", "MANUAL")),
    ("suggestionstatus", ("PENDING", "APPROVED", "REJECTED")),
)


def upgrade() -> None:
    # ALTER TYPE ADD VALUE cannot run inside a transaction in older Postgres
    # versions. Alembic's transaction_per_migration default is fine here on
    # PG 12+ (which this project targets), but the IF NOT EXISTS clause is
    # the safety belt: re-running this migration is a no-op.
    for enum_name, values in _TO_ADD:
        for value in values:
            op.execute(
                f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'"
            )


def downgrade() -> None:
    # Postgres has no ALTER TYPE DROP VALUE. Downgrade is a no-op.
    pass
