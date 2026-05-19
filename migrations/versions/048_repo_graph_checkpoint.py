"""repo_graph_checkpoint

Revision ID: 048
Revises: 047
Create Date: 2026-05-19

Adds checkpointing fields to repo_graphs so the ADR-016 pipeline can be
mid-flight resumed across container restarts / rate-limit pauses, and so
the next refresh on the same commit is a no-op:

  * is_complete       — true once a full pipeline run finished
  * processed_files   — map: rel_path -> { sites_attempted, sites_succeeded,
                        edges_added, processed_at }
  * failed_sites      — list of sites needing retry on resume

Existing rows are marked is_complete=true on upgrade: the old code only
ever wrote rows on full completion, so historically this is correct.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "048"
down_revision = "047"


def upgrade() -> None:
    op.add_column(
        "repo_graphs",
        sa.Column(
            "is_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "repo_graphs",
        sa.Column(
            "processed_files",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "repo_graphs",
        sa.Column(
            "failed_sites",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.execute("UPDATE repo_graphs SET is_complete = true")


def downgrade() -> None:
    op.drop_column("repo_graphs", "failed_sites")
    op.drop_column("repo_graphs", "processed_files")
    op.drop_column("repo_graphs", "is_complete")
