"""Clean up per-task workspace directories — ADR-015 §2 Phase 7.6.

Migration 041 TRUNCATEd ``tasks`` + ``suggestions`` but did NOT clear the
on-disk workspace dirs the orchestrator created for each task at
``/workspaces/<org_id>/task-<task_id>/`` (and legacy
``/workspaces/task-<task_id>/``). Those dirs hold an untracked
``.auto-agent/`` directory whose artefacts (design.md, backlog.json, etc.)
are invisible to ``git reset --hard``. On the next task that reused a
task_id, ``clone_repo`` reused the workspace, the stale
``.auto-agent/design.md`` survived, and the new ``_design_md_exists``
gate at ``agent/lifecycle/trio/__init__.py:421`` short-circuited the
design-approval gate by routing to ``run_initial`` instead of
``run_design``. The user-approval gate was bypassed entirely.

This migration shells out to ``rm -rf`` the per-task workspace dirs.
Idempotent — if nothing matches the glob, it's a no-op. System
workspaces (``arch-*`` for the improvement agent, ``po-*`` for the PO
worker, ``harness-*`` for onboarding, ``summary-*`` for summary tasks)
are deliberately preserved because the glob patterns are scoped to
``task-*`` only.

Revision ID: 042
Revises: 041
Create Date: 2026-05-14
"""

from __future__ import annotations

import subprocess

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


# Glob patterns to clear. Both are scoped so system workspaces (arch-, po-,
# harness-, summary-) survive. The org-prefixed pattern is restricted to
# numeric segments because ``Repo.organization_id`` is always an int —
# unrestricted ``*`` would match ``arch-cardamon/task-7`` which we must NOT
# touch.
_TASK_WORKSPACE_GLOBS = (
    "/workspaces/task-*",  # legacy layout, no org_id prefix
    "/workspaces/[0-9]*/task-*",  # current layout, /workspaces/<numeric-org>/task-<id>
)


def upgrade() -> None:
    """Remove per-task workspace dirs. No DB changes — purely filesystem."""

    for pattern in _TASK_WORKSPACE_GLOBS:
        # ``set +e`` and trailing ``true`` keep the migration tolerant when
        # nothing matches the glob (bash leaves the literal pattern in $d
        # and ``test -d`` returns non-zero — that's expected, not an error).
        cmd = f'set +e; for d in {pattern}; do test -d "$d" && rm -rf "$d"; done; true'
        subprocess.run(cmd, shell=True, check=False)


def downgrade() -> None:
    """No-op: deletion has no inverse."""
    pass
