"""Phase 2 — Organizations and memberships (multi-tenant tenant boundary).

Adds the tenant model. Every row representing customer data gains a
nullable ``organization_id`` FK pointing at ``organizations``. This
migration backfills every existing row to a single ``default`` org;
migration 027 flips the columns to NOT NULL after backfill is verified
on production.

Users<->orgs is many-to-many via ``organization_memberships``. Every
pre-existing user becomes a member of the default org (user id=1 gets
``owner``; others get ``member``).

``user_secrets`` is also re-keyed: PK changes from ``(user_id, key)`` to
``(user_id, organization_id, key)`` so a user in two orgs can hold
different credentials per org.

Repo names are no longer globally unique — uniqueness moves to
``(organization_id, name)`` so two orgs can both register a repo
called ``backend`` without colliding.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tables that gain ``organization_id``. Order matters only for backfill
# (children after parents); UPDATE statements are run in this order.
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
    # --- Tenant tables ------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS organizations (
            id          SERIAL PRIMARY KEY,
            name        VARCHAR(255) NOT NULL,
            slug        VARCHAR(64) NOT NULL UNIQUE,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS organization_memberships (
            org_id          INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role            VARCHAR(32) NOT NULL DEFAULT 'member',
            last_active_at  TIMESTAMPTZ NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id, user_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memberships_user "
        "ON organization_memberships(user_id)"
    )

    # --- Seed default org ---------------------------------------------------
    op.execute(
        """
        INSERT INTO organizations (name, slug)
        VALUES ('Default', 'default')
        ON CONFLICT (slug) DO NOTHING
        """
    )

    # --- Add nullable organization_id to every scoped table -----------------
    for table in SCOPED_TABLES:
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS organization_id INTEGER "
            f"REFERENCES organizations(id)"
        )
        op.execute(
            f"UPDATE {table} SET organization_id = "
            f"(SELECT id FROM organizations WHERE slug='default') "
            f"WHERE organization_id IS NULL"
        )
        op.execute(
            f"CREATE INDEX IF NOT EXISTS ix_{table}_org "
            f"ON {table}(organization_id)"
        )

    # --- Backfill memberships ----------------------------------------------
    # Every existing user joins the default org. user_id=1 becomes the owner
    # (legacy admin); everyone else is a member.
    op.execute(
        """
        INSERT INTO organization_memberships (org_id, user_id, role)
        SELECT
            (SELECT id FROM organizations WHERE slug='default'),
            u.id,
            CASE WHEN u.id = 1 THEN 'owner' ELSE 'member' END
        FROM users u
        ON CONFLICT (org_id, user_id) DO NOTHING
        """
    )

    # --- user_secrets PK swap ----------------------------------------------
    # Add organization_id as part of the primary key so a user can hold
    # different secrets per org. We drop the existing PK, ensure every row
    # has an organization_id (the backfill above already did this), set the
    # column NOT NULL early (safe because backfill is complete), and then
    # add the new composite PK.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'user_secrets_pkey'
            ) THEN
                ALTER TABLE user_secrets DROP CONSTRAINT user_secrets_pkey;
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE user_secrets ALTER COLUMN organization_id SET NOT NULL")
    op.execute(
        "ALTER TABLE user_secrets ADD PRIMARY KEY (user_id, organization_id, key)"
    )

    # --- Repo namespace ----------------------------------------------------
    # Drop the global UNIQUE on repos.name; add a composite UNIQUE on
    # (organization_id, name) so two orgs can have repos with the same
    # short name.
    op.execute("ALTER TABLE repos DROP CONSTRAINT IF EXISTS repos_name_key")
    op.execute("DROP INDEX IF EXISTS repos_name_key")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_repos_org_name "
        "ON repos(organization_id, name)"
    )


def downgrade() -> None:
    # Reverse the repo namespace change first.
    op.execute("DROP INDEX IF EXISTS ix_repos_org_name")
    op.execute("ALTER TABLE repos ADD CONSTRAINT repos_name_key UNIQUE (name)")

    # Restore the user_secrets PK shape (user_id, key). This is destructive
    # if multiple rows now exist with the same (user_id, key) across orgs —
    # callers must consolidate before downgrading.
    op.execute("ALTER TABLE user_secrets DROP CONSTRAINT IF EXISTS user_secrets_pkey")
    op.execute("ALTER TABLE user_secrets ALTER COLUMN organization_id DROP NOT NULL")
    op.execute("ALTER TABLE user_secrets ADD PRIMARY KEY (user_id, key)")

    # Strip organization_id from every scoped table.
    for table in reversed(SCOPED_TABLES):
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_org")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS organization_id")

    op.execute("DROP INDEX IF EXISTS ix_memberships_user")
    op.execute("DROP TABLE IF EXISTS organization_memberships")
    op.execute("DROP TABLE IF EXISTS organizations")
