"""health_loop started_by_user_id

Revision ID: 057
Revises: 056
Create Date: 2026-06-09

Adds health_loop_configs.started_by_user_id — the user who enabled the loop.
The batch coder runs as them so it uses their paired Claude + GitHub
credentials (prod has no shared host Claude; per-user pairing is the auth path).
"""

from alembic import op
import sqlalchemy as sa

revision = "057"
down_revision = "056"


def upgrade() -> None:
    op.add_column(
        "health_loop_configs",
        sa.Column(
            "started_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("health_loop_configs", "started_by_user_id")
