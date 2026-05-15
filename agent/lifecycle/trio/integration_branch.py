"""Integration-branch resolution + lazy assignment — ADR-015 Phase 7.7.

The trio integration branch lives on ``Task.integration_branch``. New
tasks (the column is NULL when ``run_trio_parent`` first prepares the
workspace) get the new ``auto-agent/<slug>-<task_id>`` shape, persisted
on first use. In-flight tasks that pre-date the rename keep their NULL
column and fall back to the legacy ``trio/<task_id>``.

Two primitives:

* :func:`resolve_integration_branch` — read-only. Returns whatever the
  column says, or the legacy default. Use anywhere a branch name is
  needed *after* it's been assigned (PR creation, checkout, etc.).
* :func:`ensure_integration_branch` — read-or-assign. The first caller
  on a fresh task name and persists the branch; subsequent callers see
  the same value. Use anywhere that's the first to need a branch name
  (architect's workspace prep is the canonical entry point).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from agent.lifecycle.trio.branch_name import integration_branch_name
from shared.database import async_session
from shared.models import Task


def _legacy_branch(task_id: int) -> str:
    """The pre-Phase-7.7 shape. Falls back to this for tasks whose
    ``integration_branch`` column is NULL — including in-flight tasks
    created before the rename landed."""

    return f"trio/{task_id}"


def resolve_integration_branch(parent: Any) -> str:
    """Return the integration branch name for ``parent`` without touching the DB.

    ``parent`` is anything with ``.id`` and ``.integration_branch``
    attributes (a ``Task`` ORM row or a ``SimpleNamespace`` in tests).
    Empty / NULL ``integration_branch`` ⇒ legacy ``trio/<id>``.
    """

    stored = getattr(parent, "integration_branch", None)
    if stored:
        return stored
    return _legacy_branch(parent.id)


async def ensure_integration_branch(parent_id: int, title: str | None) -> str:
    """Read or assign the integration branch for a task.

    First call on a fresh task: computes ``integration_branch_name``,
    persists it, returns the new value. Subsequent calls: return what
    was already persisted, leaving the column untouched.

    Pre-Phase-7.7 tasks that already had local + remote ``trio/<id>``
    branches must NOT be renamed mid-flight, so this helper has a
    self-imposed invariant: it never *overwrites* a NULL with the new
    shape on a task whose status already implies an in-progress trio
    cycle? — No. The simpler rule is: once written, never overwrite.
    The "in-flight" tasks are the ones where this helper has *never
    been called*, so they stay NULL and the resolver hands back
    ``trio/<id>`` for as long as they live.

    Returns the canonical branch name. Always non-empty.
    """

    async with async_session() as s:
        row = (await s.execute(select(Task).where(Task.id == parent_id))).scalar_one()
        if row.integration_branch:
            return row.integration_branch
        branch = integration_branch_name(parent_id, title)
        row.integration_branch = branch
        await s.commit()
        return branch


__all__ = [
    "ensure_integration_branch",
    "resolve_integration_branch",
]
