"""Integration branch naming for trio tasks — ADR-015 Phase 7.7.

The integration branch is the long-lived branch every child PR merges
into during a trio cycle; the final PR opens from it back to the target
branch. Phase 7.7 renames it from ``trio/<task_id>`` to
``auto-agent/<slug>-<task_id>`` so the branch tells an operator
something about the task at a glance.

Backwards compatibility is handled by the orchestrator: the new name is
stored on ``Task.integration_branch`` the first time it's needed, and
in-flight tasks with a NULL column fall back to ``trio/<id>``. This
module is pure — it just derives the new name from ``(task_id, title)``.
"""

from __future__ import annotations

import re

_BRANCH_PREFIX = "auto-agent"
_MAX_SLUG_LEN = 50

# Anything that isn't a lowercase letter or digit becomes a separator.
# ASCII-only on purpose: git branch names tolerate unicode in theory but
# CI and shell tooling stumble on it in practice. Non-ASCII title chars
# drop out → callers fall back to the ``task-<id>`` shape if nothing
# survives.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify_for_branch(title: str | None) -> str:
    """Turn ``title`` into a kebab-case slug ≤ ``_MAX_SLUG_LEN`` chars.

    Lowercase, collapse non-alphanumeric runs to ``-``, strip leading
    and trailing ``-``, truncate to the budget, and strip again so a
    truncation that lands on a delimiter doesn't leave a trailing dash.
    Returns the empty string when nothing alphanumeric survives.
    """

    if not title:
        return ""
    lower = title.lower()
    collapsed = _NON_ALNUM.sub("-", lower).strip("-")
    if not collapsed:
        return ""
    if len(collapsed) > _MAX_SLUG_LEN:
        collapsed = collapsed[:_MAX_SLUG_LEN].rstrip("-")
    return collapsed


def integration_branch_name(task_id: int, title: str | None) -> str:
    """Return ``auto-agent/<slug>-<task_id>`` for the task.

    Falls back to ``auto-agent/task-<task_id>`` when the title is empty
    or has no characters that survive slugification.
    """

    slug = slugify_for_branch(title)
    if not slug:
        return f"{_BRANCH_PREFIX}/task-{task_id}"
    return f"{_BRANCH_PREFIX}/{slug}-{task_id}"


def init_branch_name(integration_branch: str) -> str:
    """Head-branch name for the architect's initial PR — a *sibling* of
    ``integration_branch``, NOT a sub-path.

    Why: git stores refs as files on disk (``refs/heads/foo``) so a
    branch named ``foo/init`` would need ``foo`` to be a directory. When
    both exist, ``git checkout -B foo/init`` fails with "cannot lock
    ref … exists; cannot create …" and the subsequent push reports
    "src refspec foo/init does not match any". A dash separator keeps
    the names flat siblings.
    """
    return f"{integration_branch}-init"


def consult_branch_name(integration_branch: str, ts: int) -> str:
    """Head-branch name for the consulting-architect cycle — sibling of
    ``integration_branch``. See :func:`init_branch_name` for the
    refs D/F-conflict rationale."""
    return f"{integration_branch}-consult-{ts}"


__all__ = [
    "consult_branch_name",
    "init_branch_name",
    "integration_branch_name",
    "slugify_for_branch",
]
