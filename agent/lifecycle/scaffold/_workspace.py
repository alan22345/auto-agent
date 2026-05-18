"""Workspace prep for the scaffold parent task.

The scaffold parent itself doesn't open PRs — its only job is to write
artefacts under ``.auto-agent/`` (intent.md, ADRs, approvals, final
verification). We reuse the trio's ``_prepare_parent_workspace`` which
clones the repo (or seeds an empty workspace for repo-less tasks) and
checks out the integration branch, so the artefacts live on a real git
branch and survive parent-task resumption.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shared.models import Task


async def prepare_scaffold_workspace(task: Task) -> str:
    """Return a workspace path the scaffold parent can write artefacts to.

    Delegates to ``agent.lifecycle.trio.architect._prepare_parent_workspace``
    so we don't duplicate the clone/init logic. The trio's branch shape
    (``auto-agent/<slug>-<id>``) is what we want here too — the
    artefacts will land on that branch.
    """

    from agent.lifecycle.trio.architect import _prepare_parent_workspace

    workspace = await _prepare_parent_workspace(task)
    return workspace.root if hasattr(workspace, "root") else str(workspace)
