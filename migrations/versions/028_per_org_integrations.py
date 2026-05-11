"""028 — per-org Slack + GitHub installations + webhook secrets

Adds three tables:
  * slack_installations  (1:1 with organizations, keyed by team_id)
  * github_installations (1:1 with organizations, keyed by installation_id)
  * webhook_secrets      (composite PK on (org_id, source))

Token columns are pgcrypto-encrypted BYTEA. Reuses the SECRETS_PASSPHRASE
already in use by user_secrets — the deploy MUST run with the same
passphrase across 027 → 028 or every stored token decodes to garbage.

Revision ID: 028
Revises: 027
Create Date: 2026-05-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "slack_installations",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("team_id", sa.String(length=32), nullable=False),
        sa.Column("team_name", sa.String(length=255), nullable=True),
        sa.Column("bot_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("bot_user_id", sa.String(length=32), nullable=False),
        sa.Column("app_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("installed_by_slack_user_id", sa.String(length=32), nullable=True),
        sa.Column(
            "installed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id"),
        sa.UniqueConstraint("team_id", name="uq_slack_installations_team_id"),
    )

    op.create_table(
        "github_installations",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.String(length=128), nullable=False),
        sa.Column("account_type", sa.String(length=32), nullable=False),
        sa.Column(
            "installed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id"),
        sa.UniqueConstraint(
            "installation_id", name="uq_github_installations_installation_id"
        ),
    )

    op.create_table(
        "webhook_secrets",
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("secret_enc", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("org_id", "source"),
    )

    op.create_index(
        "ix_slack_installations_team_id",
        "slack_installations",
        ["team_id"],
    )
    op.create_index(
        "ix_github_installations_installation_id",
        "github_installations",
        ["installation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_github_installations_installation_id", "github_installations")
    op.drop_index("ix_slack_installations_team_id", "slack_installations")
    op.drop_table("webhook_secrets")
    op.drop_table("github_installations")
    op.drop_table("slack_installations")
