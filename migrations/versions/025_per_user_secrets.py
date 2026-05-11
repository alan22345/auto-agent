"""Per-user secrets table + signup/email columns on users.

Revision ID: 025
Revises: 024
Create Date: 2026-05-09

Phase 1 of the multi-tenant SaaS roadmap. Stores arbitrary per-user secrets
(GitHub PAT, Anthropic API key, ...) encrypted at rest with pgcrypto's
``pgp_sym_encrypt``. Plaintext is held only inside the SQL statement that
sets/reads it — Python never sees the encrypted bytes.

Also adds the columns needed for self-serve signup with email verification:
``users.email``, ``users.email_verified_at``, ``users.signup_token``.
``email`` is nullable because legacy admin/seeded users have none.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_secrets (
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            key        VARCHAR(64) NOT NULL,
            value_enc  BYTEA NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, key)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_user_secrets_user ON user_secrets (user_id)")

    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255) NULL")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email) WHERE email IS NOT NULL"
    )
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ NULL")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS signup_token VARCHAR(64) NULL")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_signup_token "
        "ON users (signup_token) WHERE signup_token IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_signup_token")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS signup_token")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS email_verified_at")
    op.execute("DROP INDEX IF EXISTS ix_users_email")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS email")

    op.execute("DROP INDEX IF EXISTS ix_user_secrets_user")
    op.execute("DROP TABLE IF EXISTS user_secrets")
    # pgcrypto extension left in place — other migrations may rely on it.
