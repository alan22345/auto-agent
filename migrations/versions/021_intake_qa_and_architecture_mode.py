"""Add tasks.intake_qa and architecture-mode columns on freeform_configs.

Two related changes in one migration because they ship together:

  - tasks.intake_qa: JSONB list of {question, answer} pairs accumulated during
    the grill-before-planning phase. NULL means "grilling not yet started";
    empty list [] means "grilling complete or skipped" (e.g. simple tasks,
    architecture-suggestion-derived tasks).

  - freeform_configs.{architecture_mode, architecture_cron, last_architecture_at,
    architecture_knowledge}: per-repo Architecture Mode (analogous to PO mode).
    When enabled, the architect_analyzer cron runs the
    improve-codebase-architecture skill against the repo and produces
    deepening-opportunity suggestions.

Revision ID: 021
Revises: 020
Create Date: 2026-05-02
"""
from typing import Sequence, Union

from alembic import op


revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS intake_qa JSONB")

    op.execute(
        "ALTER TABLE freeform_configs "
        "ADD COLUMN IF NOT EXISTS architecture_mode BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE freeform_configs "
        "ADD COLUMN IF NOT EXISTS architecture_cron VARCHAR(100) NOT NULL DEFAULT '0 9 * * 1'"
    )
    op.execute(
        "ALTER TABLE freeform_configs "
        "ADD COLUMN IF NOT EXISTS last_architecture_at TIMESTAMP WITH TIME ZONE"
    )
    op.execute(
        "ALTER TABLE freeform_configs "
        "ADD COLUMN IF NOT EXISTS architecture_knowledge TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE freeform_configs DROP COLUMN IF EXISTS architecture_knowledge")
    op.execute("ALTER TABLE freeform_configs DROP COLUMN IF EXISTS last_architecture_at")
    op.execute("ALTER TABLE freeform_configs DROP COLUMN IF EXISTS architecture_cron")
    op.execute("ALTER TABLE freeform_configs DROP COLUMN IF EXISTS architecture_mode")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS intake_qa")
