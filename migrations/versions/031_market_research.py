"""Market research — MarketBrief table + Suggestion.evidence_urls + FreeformConfig age.

Adds the schema for sub-project A of the PO/freeform overhaul. See
docs/superpowers/specs/2026-05-12-bigger-po-market-research-design.md.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_briefs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "repo_id",
            sa.Integer,
            sa.ForeignKey("repos.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "organization_id",
            sa.Integer,
            sa.ForeignKey("organizations.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("product_category", sa.Text, nullable=True),
        sa.Column(
            "competitors",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "findings",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "modality_gaps",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "strategic_themes",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "raw_sources",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "partial", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("agent_turns", sa.Integer, nullable=False, server_default="0"),
    )

    op.add_column(
        "freeform_configs",
        sa.Column(
            "last_market_research_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "freeform_configs",
        sa.Column(
            "market_brief_max_age_days",
            sa.Integer,
            nullable=False,
            server_default="7",
        ),
    )

    op.add_column(
        "suggestions",
        sa.Column(
            "evidence_urls",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "suggestions",
        sa.Column(
            "brief_id",
            sa.Integer,
            sa.ForeignKey("market_briefs.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("suggestions", "brief_id")
    op.drop_column("suggestions", "evidence_urls")
    op.drop_column("freeform_configs", "market_brief_max_age_days")
    op.drop_column("freeform_configs", "last_market_research_at")
    op.drop_table("market_briefs")
