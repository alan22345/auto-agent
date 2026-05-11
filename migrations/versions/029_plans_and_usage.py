"""029 — plans + per-org plan_id + usage_events

Adds plan tiers, attaches each organization to a plan (default 'free'),
and a usage_events fact table for per-call LLM accounting.

Seeded plan rows are intentionally hardcoded — Phase 4 ships with three
tiers (free/pro/team). Phase 5 may add columns or rows; do not remove
the seeded rows in any future migration without an explicit data plan.

Revision ID: 029
Revises: 028
Create Date: 2026-05-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "029"
down_revision: Union[str, None] = "028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'blocked_on_quota'")

    plans = op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("max_concurrent_tasks", sa.Integer(), nullable=False),
        sa.Column("max_tasks_per_day", sa.Integer(), nullable=False),
        sa.Column("max_input_tokens_per_day", sa.BigInteger(), nullable=False),
        sa.Column("max_output_tokens_per_day", sa.BigInteger(), nullable=False),
        sa.Column("max_members", sa.Integer(), nullable=False),
        sa.Column("monthly_price_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("name", name="uq_plans_name"),
    )

    op.bulk_insert(
        plans,
        [
            {"name": "free", "max_concurrent_tasks": 1, "max_tasks_per_day": 5,
             "max_input_tokens_per_day": 1_000_000, "max_output_tokens_per_day": 250_000,
             "max_members": 3, "monthly_price_cents": 0},
            {"name": "pro", "max_concurrent_tasks": 3, "max_tasks_per_day": 50,
             "max_input_tokens_per_day": 10_000_000, "max_output_tokens_per_day": 2_500_000,
             "max_members": 5, "monthly_price_cents": 0},
            {"name": "team", "max_concurrent_tasks": 5, "max_tasks_per_day": 200,
             "max_input_tokens_per_day": 50_000_000, "max_output_tokens_per_day": 12_500_000,
             "max_members": 25, "monthly_price_cents": 0},
        ],
    )

    op.add_column(
        "organizations",
        sa.Column("plan_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_organizations_plan_id",
        "organizations",
        "plans",
        ["plan_id"],
        ["id"],
    )
    op.execute(
        "UPDATE organizations SET plan_id = (SELECT id FROM plans WHERE name = 'free')"
    )
    op.alter_column("organizations", "plan_id", nullable=False)

    op.create_table(
        "usage_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_cents", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_usage_events_org_time",
        "usage_events",
        ["org_id", sa.text("occurred_at DESC")],
    )


def downgrade() -> None:
    # Note: Postgres does not support removing an enum value; 'blocked_on_quota'
    # remains in the taskstatus type after downgrade. Harmless — no rows reference
    # it without the Phase 4 quota gate.
    op.drop_index("ix_usage_events_org_time", "usage_events")
    op.drop_table("usage_events")
    op.drop_constraint("fk_organizations_plan_id", "organizations", type_="foreignkey")
    op.drop_column("organizations", "plan_id")
    op.drop_table("plans")
