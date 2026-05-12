"""030 — messenger_conversations + user_focus

Source-agnostic durable conversation history for messenger DMs (Slack
today, Telegram next), plus a per-user focus pointer with 24h TTL.

Revision ID: 030
Revises: 029
Create Date: 2026-05-12
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "messenger_conversations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("focus_kind", sa.String(length=32), nullable=False),
        sa.Column("focus_id", sa.BigInteger(), nullable=True),
        sa.Column("messages_json", JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "user_id", "source", "focus_kind", "focus_id",
            name="uq_msgconv_user_source_focus",
        ),
    )
    op.create_index(
        "ix_msgconv_user_recent",
        "messenger_conversations",
        ["user_id", "last_active_at"],
        postgresql_using="btree",
    )

    op.create_table(
        "user_focus",
        sa.Column(
            "user_id", sa.Integer(),
            sa.ForeignKey("users.id"),
            primary_key=True,
        ),
        sa.Column("focus_kind", sa.String(length=32), nullable=False),
        sa.Column("focus_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "set_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_focus")
    op.drop_index("ix_msgconv_user_recent", table_name="messenger_conversations")
    op.drop_table("messenger_conversations")
