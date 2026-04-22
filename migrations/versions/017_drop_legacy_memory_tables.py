"""drop legacy memory tables (replaced by team-memory)

Revision ID: 017
Revises: 016
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_memory_edges_target_id", table_name="memory_edges")
    op.drop_index("ix_memory_edges_source_id", table_name="memory_edges")
    op.drop_index("ix_memory_nodes_node_type", table_name="memory_nodes")
    op.drop_index("ix_memory_nodes_name", table_name="memory_nodes")
    op.drop_table("memory_edges")
    op.drop_table("memory_nodes")


def downgrade() -> None:
    # No-op: dropped tables are not recoverable here. If rollback needed,
    # re-run migration 016 manually or restore from backup.
    pass
