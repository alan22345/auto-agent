"""Phase 2 — flip organization_id to NOT NULL + add query indexes.

Migration 026 added nullable ``organization_id`` columns and backfilled
every existing row to the default org. This migration:

* Asserts every scoped table has zero NULLs (raises if not — operator
  intervention required before re-running).
* Flips ``organization_id`` to NOT NULL on every scoped table.
* Adds composite indexes for the most common query shapes (e.g.
  ``WHERE organization_id = X AND status = Y``).

The NOT NULL constraint is the load-bearing tenant guarantee: future
inserts that forget to stamp ``organization_id`` will fail at the DB
layer rather than silently leaking across orgs.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "027"
down_revision: Union[str, None] = "026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


SCOPED_TABLES = [
    "users",
    "repos",
    "tasks",
    "scheduled_tasks",
    "suggestions",
    "freeform_configs",
    "search_sessions",
    "user_secrets",
]


def upgrade() -> None:
    # Sanity check — refuse to flip NOT NULL while any rows still have a
    # NULL organization_id (would fail the ALTER anyway, but a friendlier
    # error message helps the operator).
    for table in SCOPED_TABLES:
        op.execute(
            f"""
            DO $$
            DECLARE
                n INT;
            BEGIN
                SELECT COUNT(*) INTO n FROM {table} WHERE organization_id IS NULL;
                IF n > 0 THEN
                    RAISE EXCEPTION
                        '{table} has % rows with NULL organization_id — run 026 backfill first', n;
                END IF;
            END $$;
            """
        )

    for table in SCOPED_TABLES:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN organization_id SET NOT NULL"
        )

    # Performance indexes for the most common scoped query shapes.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tasks_org_status "
        "ON tasks(organization_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tasks_org_created "
        "ON tasks(organization_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_suggestions_org_status "
        "ON suggestions(organization_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_search_sessions_org_user "
        "ON search_sessions(organization_id, user_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_search_sessions_org_user")
    op.execute("DROP INDEX IF EXISTS ix_suggestions_org_status")
    op.execute("DROP INDEX IF EXISTS ix_tasks_org_created")
    op.execute("DROP INDEX IF EXISTS ix_tasks_org_status")

    for table in reversed(SCOPED_TABLES):
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN organization_id DROP NOT NULL"
        )
