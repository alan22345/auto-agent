"""verify_review_attempts

Revision ID: 032_verify_review_attempts
Revises: 031_market_research
Create Date: 2026-05-12

Adds VERIFYING task status, Task.affected_routes, FreeformConfig.run_command,
and the verify_attempts + review_attempts tables.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "032"
down_revision = "031"


def upgrade() -> None:
    # Match migration 029's convention: SQLAlchemy's Enum(TaskStatus) serializes
    # by NAME (uppercase), but the taskstatus type has historically mixed
    # uppercase and lowercase variants. Add both so any legacy raw-SQL path that
    # writes the lowercase value continues to work.
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'VERIFYING'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'verifying'")

    op.add_column(
        "tasks",
        sa.Column(
            "affected_routes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    op.add_column("freeform_configs", sa.Column("run_command", sa.Text(), nullable=True))

    op.create_table(
        "verify_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("boot_check", sa.String(16), nullable=True),
        sa.Column("intent_check", sa.String(16), nullable=True),
        sa.Column("intent_judgment", sa.Text(), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("log_tail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("task_id", "cycle", name="ix_verify_attempts_task_cycle"),
    )

    op.create_table(
        "review_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("code_review_verdict", sa.Text(), nullable=True),
        sa.Column("ui_check", sa.String(16), nullable=True),
        sa.Column("ui_judgment", sa.Text(), nullable=True),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("log_tail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("task_id", "cycle", name="ix_review_attempts_task_cycle"),
    )


def downgrade() -> None:
    op.drop_table("review_attempts")
    op.drop_table("verify_attempts")
    op.drop_column("freeform_configs", "run_command")
    op.drop_column("tasks", "affected_routes")
    # Postgres does not support removing enum values — leave 'verifying' in place.
