"""repo_graph_flow_json

Revision ID: 053
Revises: 052
Create Date: 2026-05-22

Adds a nullable flow_json JSONB column to repo_graphs. Phase 1 of the
capability/flow map spec persists the result of forward-tracing flows
from entry points to terminal side effects. Nullable because existing
RepoGraph rows do not have a derivation; the API surface treats null
as "compute flows now" and a populated blob as "show them."

See docs/superpowers/specs/2026-05-22-repo-graph-capability-flow-map-design.md
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "053"
down_revision = "052"


def upgrade() -> None:
    op.add_column(
        "repo_graphs",
        sa.Column(
            "flow_json",
            postgresql.JSONB(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("repo_graphs", "flow_json")
