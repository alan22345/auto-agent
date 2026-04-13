"""Add FREEFORM (uppercase) to the tasksource enum.

Migration 008 added 'freeform' (lowercase) but SQLAlchemy serializes Python
enum members by their NAME (uppercase), so any insert using TaskSource.FREEFORM
fails. Adding the uppercase variant aligns with the convention used by all
other tasksource values (MANUAL, SLACK, LINEAR, TELEGRAM).

Revision ID: 010
Revises: 009
Create Date: 2026-04-11
"""
from typing import Sequence, Union

from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE tasksource ADD VALUE IF NOT EXISTS 'FREEFORM'")


def downgrade() -> None:
    # Postgres doesn't support removing enum values without recreating the type.
    pass
