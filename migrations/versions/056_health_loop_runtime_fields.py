"""health_loop runtime fields

Revision ID: 056
Revises: 055
Create Date: 2026-06-09

Adds the runtime/status columns the auto-heal supervisor + batch handler need
on top of the config row from 055:
  - addressed_finding_hashes — dedup + no-retry set (merged OR parked).
  - current_batch            — in-flight findings for the status strip.
  - merged_count / parked_count — running tallies.
  - cleanup_pr_url           — the standing cleanup → main PR link.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "056"
down_revision = "055"


def upgrade() -> None:
    op.add_column(
        "health_loop_configs",
        sa.Column(
            "addressed_finding_hashes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "health_loop_configs",
        sa.Column(
            "current_batch",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "health_loop_configs",
        sa.Column("merged_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "health_loop_configs",
        sa.Column("parked_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "health_loop_configs",
        sa.Column("cleanup_pr_url", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("health_loop_configs", "cleanup_pr_url")
    op.drop_column("health_loop_configs", "parked_count")
    op.drop_column("health_loop_configs", "merged_count")
    op.drop_column("health_loop_configs", "current_batch")
    op.drop_column("health_loop_configs", "addressed_finding_hashes")
