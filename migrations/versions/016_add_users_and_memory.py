"""Add users table, graph memory tables, and user FK on tasks.

Revision ID: 016
Revises: 015
Create Date: 2026-04-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Users table
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(100), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
    )

    # Memory nodes table
    op.create_table(
        "memory_nodes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("node_type", sa.String(100), nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_by_task_id", sa.Integer, sa.ForeignKey("tasks.id"), nullable=True),
    )

    # Memory edges table
    op.create_table(
        "memory_edges",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_id", UUID(as_uuid=True), sa.ForeignKey("memory_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("relation", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # Add user FK to tasks
    op.add_column("tasks", sa.Column("created_by_user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True))

    # Indexes for common queries
    op.create_index("ix_memory_nodes_name", "memory_nodes", ["name"])
    op.create_index("ix_memory_nodes_node_type", "memory_nodes", ["node_type"])
    op.create_index("ix_memory_edges_source_id", "memory_edges", ["source_id"])
    op.create_index("ix_memory_edges_target_id", "memory_edges", ["target_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_edges_target_id")
    op.drop_index("ix_memory_edges_source_id")
    op.drop_index("ix_memory_nodes_node_type")
    op.drop_index("ix_memory_nodes_name")
    op.drop_column("tasks", "created_by_user_id")
    op.drop_table("memory_edges")
    op.drop_table("memory_nodes")
    op.drop_table("users")
