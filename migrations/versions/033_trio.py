"""trio schema — TaskStatus + TrioPhase enums, Task columns, attempt tables

Revision ID: 033
Revises: 032
Create Date: 2026-05-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "033"
down_revision = "032"


def upgrade() -> None:
    # Loose-end: migration 012 added COMPLEX_LARGE uppercase only.
    # The trio router writes `complex_large` (the lowercase value), so add it.
    op.execute("ALTER TYPE taskcomplexity ADD VALUE IF NOT EXISTS 'complex_large'")

    # Add TaskStatus enum values (idempotent, matches 032's pattern).
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'TRIO_EXECUTING'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'trio_executing'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'TRIO_REVIEW'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'trio_review'")

    # New enums
    op.execute(
        "CREATE TYPE triophase AS ENUM ("
        "'ARCHITECTING', 'architecting', "
        "'AWAITING_BUILDER', 'awaiting_builder', "
        "'ARCHITECT_CHECKPOINT', 'architect_checkpoint'"
        ")"
    )
    op.execute(
        "CREATE TYPE architect_phase AS ENUM ("
        "'INITIAL', 'initial', "
        "'CONSULT', 'consult', "
        "'CHECKPOINT', 'checkpoint', "
        "'REVISION', 'revision'"
        ")"
    )

    # Task columns
    op.add_column("tasks", sa.Column("parent_task_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_tasks_parent_task_id", "tasks", "tasks", ["parent_task_id"], ["id"],
    )
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])
    op.add_column(
        "tasks",
        sa.Column("trio_phase", postgresql.ENUM(name="triophase", create_type=False), nullable=True),
    )
    op.add_column("tasks", sa.Column("trio_backlog", postgresql.JSONB(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column(
            "consulting_architect",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # architect_attempts
    op.create_table(
        "architect_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True,
        ),
        sa.Column(
            "phase",
            postgresql.ENUM(name="architect_phase", create_type=False),
            nullable=False,
        ),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("decision", postgresql.JSONB(), nullable=True),
        sa.Column("consult_question", sa.Text(), nullable=True),
        sa.Column("consult_why", sa.Text(), nullable=True),
        sa.Column("architecture_md_after", sa.Text(), nullable=True),
        sa.Column("commit_sha", sa.String(40), nullable=True),
        sa.Column(
            "tool_calls", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # trio_review_attempts
    op.create_table(
        "trio_review_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "task_id", sa.Integer(),
            sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True,
        ),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "tool_calls", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("trio_review_attempts")
    op.drop_table("architect_attempts")
    op.drop_column("tasks", "consulting_architect")
    op.drop_column("tasks", "trio_backlog")
    op.drop_column("tasks", "trio_phase")
    op.drop_index("ix_tasks_parent_task_id", table_name="tasks")
    op.drop_constraint("fk_tasks_parent_task_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "parent_task_id")
    op.execute("DROP TYPE architect_phase")
    op.execute("DROP TYPE triophase")
    # Postgres does not support removing enum values — leave trio_executing/trio_review.
