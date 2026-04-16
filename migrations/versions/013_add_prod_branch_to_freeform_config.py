"""Add prod_branch to freeform_configs.

The existing `dev_branch` column was used as the PR target for freeform tasks,
and repos without a true dev branch ended up with `dev_branch` set to their
production branch name (e.g. "prod"), which made the schema confusing and
correlated with a safety bug (PRs auto-merging to prod).

We now model both branches explicitly:
  - prod_branch: the production branch (PR target when freeform is disabled
                 OR when promoting from dev to prod). Defaults to the repo's
                 default_branch.
  - dev_branch:  the working branch freeform PRs target and auto-merge into.
                 If it doesn't exist on the remote, the agent creates it from
                 prod_branch before cloning.

For existing rows, `prod_branch` is backfilled with the repo's default_branch.

Revision ID: 013
Revises: 012
Create Date: 2026-04-16
"""
from typing import Sequence, Union

from alembic import op


revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add the new column
    op.execute(
        "ALTER TABLE freeform_configs ADD COLUMN IF NOT EXISTS prod_branch VARCHAR(128)"
    )
    # Backfill from the repo's default_branch — if a freeform config exists,
    # its repo must exist, so this join is safe.
    op.execute(
        """
        UPDATE freeform_configs fc
        SET prod_branch = r.default_branch
        FROM repos r
        WHERE fc.repo_id = r.id
          AND fc.prod_branch IS NULL
        """
    )
    # Fallback for any rows without a matching repo — shouldn't happen but
    # keeps the column non-null in the next step.
    op.execute(
        "UPDATE freeform_configs SET prod_branch = 'main' WHERE prod_branch IS NULL"
    )
    # Now lock it down.
    op.execute("ALTER TABLE freeform_configs ALTER COLUMN prod_branch SET NOT NULL")
    op.execute(
        "ALTER TABLE freeform_configs ALTER COLUMN prod_branch SET DEFAULT 'main'"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE freeform_configs DROP COLUMN IF EXISTS prod_branch")
