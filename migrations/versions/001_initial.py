"""Initial schema — repos, tasks, task_history.

Revision ID: 001
Revises:
Create Date: 2026-04-02
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "repos",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("default_branch", sa.String(128), server_default="main"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    task_complexity = sa.Enum("simple", "complex", name="taskcomplexity")
    task_status = sa.Enum(
        "intake", "classifying", "queued", "planning", "awaiting_approval",
        "coding", "pr_created", "awaiting_ci", "awaiting_review",
        "done", "blocked", "failed",
        name="taskstatus",
    )
    task_source = sa.Enum("slack", "linear", "telegram", "manual", name="tasksource")

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text(), server_default=""),
        sa.Column("source", task_source, nullable=False),
        sa.Column("source_id", sa.String(255), server_default=""),
        sa.Column("status", task_status, server_default="intake", nullable=False),
        sa.Column("complexity", task_complexity, nullable=True),
        sa.Column("repo_id", sa.Integer(), sa.ForeignKey("repos.id"), nullable=True),
        sa.Column("branch_name", sa.String(255), nullable=True),
        sa.Column("pr_url", sa.String(512), nullable=True),
        sa.Column("plan", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "task_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("from_status", task_status, nullable=True),
        sa.Column("to_status", task_status, nullable=False),
        sa.Column("message", sa.Text(), server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("task_history")
    op.drop_table("tasks")
    op.drop_table("repos")
    sa.Enum(name="taskcomplexity").drop(op.get_bind())
    sa.Enum(name="taskstatus").drop(op.get_bind())
    sa.Enum(name="tasksource").drop(op.get_bind())
