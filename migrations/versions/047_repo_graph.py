"""repo_graph

Revision ID: 047_repo_graph
Revises: 046_scaffold_complexity_and_states
Create Date: 2026-05-15

Renumbered from 033 → 047 during the local-main ↔ origin-main reconciliation
(2026-05-18). Two migrations collided on revision 033 (this one from ADR-016
and 033_trio from ADR-013); the trio chain had already been applied on the
deployed VM but this one had not, so repositioning at the end of the chain
is safe and lets `alembic upgrade head` apply it on top of the existing state.

Adds the Phase 1 scaffolding tables for ADR-016 (the code-graph feature):

    * ``repo_graph_configs`` — per-repo opt-in settings (one row per repo
      with graph analysis enabled). Owned by `RepoGraphConfig` in
      `shared/models.py`.
    * ``repo_graphs`` — one row per completed graph analysis. Schema is
      created so the FK from `repo_graph_configs.last_analysis_id` resolves,
      but the table stays empty until the Phase 2 analyser is wired in.

The FK from `repo_graph_configs.last_analysis_id` -> `repo_graphs.id` is
NULL-able and uses `ON DELETE SET NULL`, so removing stale analysis rows
never cascades into config rows.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "047"
down_revision = "046"


def upgrade() -> None:
    # ``repo_graphs`` first — ``repo_graph_configs.last_analysis_id`` FKs to it.
    op.create_table(
        "repo_graphs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "repo_id",
            sa.Integer(),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("analyser_version", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="ok",
        ),
        sa.Column("graph_json", postgresql.JSONB(), nullable=False),
    )

    op.create_table(
        "repo_graph_configs",
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
        sa.Column("analysis_branch", sa.String(255), nullable=False),
        sa.Column(
            "analyser_version",
            sa.String(64),
            nullable=False,
            server_default="",
        ),
        sa.Column("workspace_path", sa.String(1024), nullable=False),
        sa.Column(
            "last_analysis_id",
            sa.Integer(),
            sa.ForeignKey("repo_graphs.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
    op.drop_table("repo_graph_configs")
    op.drop_table("repo_graphs")
