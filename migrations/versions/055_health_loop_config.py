"""health_loop_config

Revision ID: 055
Revises: 054
Create Date: 2026-06-09

Creates health_loop_configs — per-repo auto-heal loop settings (ADR /
docs/superpowers/specs/2026-06-09-auto-heal-loop-design.md). One row per
repo; repo_id PK (1:1). suppressed_finding_hashes is the won't-fix list.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "055"
down_revision = "054"


def upgrade() -> None:
    op.create_table(
        "health_loop_configs",
        sa.Column(
            "repo_id",
            sa.Integer(),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "organization_id",
            sa.Integer(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "cleanup_branch",
            sa.String(length=255),
            nullable=False,
            server_default="auto-agent/health-cleanup",
        ),
        sa.Column("batch_size", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="idle"),
        sa.Column(
            "suppressed_finding_hashes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "supervisor_task_id",
            sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
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


def downgrade() -> None:
    op.drop_table("health_loop_configs")
