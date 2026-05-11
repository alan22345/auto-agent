"""Add per-user messaging-platform identifiers to users.

Revision ID: 024
Revises: 023
Create Date: 2026-05-08

Background: notifications were fanning out via a single global TELEGRAM_CHAT_ID
env var (admin's personal chat), so every team member's task pinged the admin.
Storing the recipient identity per-user lets the notifier route to the actual
task owner.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id "
        "VARCHAR(64) NULL"
    )
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS slack_user_id "
        "VARCHAR(64) NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_telegram_chat_id "
        "ON users (telegram_chat_id) WHERE telegram_chat_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_slack_user_id "
        "ON users (slack_user_id) WHERE slack_user_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_users_telegram_chat_id")
    op.execute("DROP INDEX IF EXISTS ix_users_slack_user_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS slack_user_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS telegram_chat_id")
