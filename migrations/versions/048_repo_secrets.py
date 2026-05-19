"""repo_secrets

Revision ID: 048
Revises: 047
Create Date: 2026-05-19

Adds the per-repo project secrets vault table for ADR-019 (T1):

    * ``repo_secrets`` — per-repo encrypted project credentials.
      value_enc is nullable so architect-declared rows can exist as
      placeholders before the user populates them.
      source is 'user' | 'architect_required'.

Unique constraint on (repo_id, key) — one value per key per repo.
Index on repo_id and organization_id for efficient per-repo and per-org queries.
"""

from alembic import op
import sqlalchemy as sa


revision = "048"
down_revision = "047"


def upgrade() -> None:
    op.create_table(
        "repo_secrets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "repo_id",
            sa.Integer(),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value_enc", sa.LargeBinary(), nullable=True),
        sa.Column("source", sa.String(32), nullable=False, server_default="user"),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_repo_secrets_repo_id", "repo_secrets", ["repo_id"])
    op.create_index("ix_repo_secrets_organization_id", "repo_secrets", ["organization_id"])
    op.create_unique_constraint(
        "uq_repo_secrets_repo_key", "repo_secrets", ["repo_id", "key"]
    )


def downgrade() -> None:
    op.drop_table("repo_secrets")
