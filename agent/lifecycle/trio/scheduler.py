"""Trio child-task scheduler. Pure orchestration, no LLM.

Two public functions:

* ``dispatch_next(parent)`` picks the next pending work item from the
  parent's ``trio_backlog``, inserts a child ``Task`` row, marks the
  backlog item ``in_progress`` with ``assigned_task_id`` pointing at the
  child, and returns the child. It is **idempotent**: if the next item
  is already ``in_progress`` and points at an existing Task, that Task is
  returned without inserting a duplicate. This keeps recovery from a
  process crash safe.

* ``await_child(parent, child)`` blocks until the child reaches a
  terminal status (``DONE`` / ``FAILED`` / ``BLOCKED``) and returns the
  refreshed Task.

There is no general async ``subscribe`` seam in :mod:`shared.events` —
production events go to a Redis Stream that is consumed by the
orchestrator's stream reader. Rather than reach into that consumer from
here, we poll the DB on a short interval. The interval is configurable
via the module-level ``_POLL_INTERVAL_S`` constant so tests can shrink
it.
"""
from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select

from shared.database import async_session
from shared.models import Task, TaskComplexity, TaskStatus

log = structlog.get_logger()

# How often ``await_child`` polls the DB for terminal status. Module-level
# so tests can monkeypatch it to a smaller value.
_POLL_INTERVAL_S = 0.5

_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.BLOCKED},
)


def _next_pending(backlog: list[dict]) -> tuple[int, dict] | None:
    for idx, item in enumerate(backlog):
        if item.get("status") == "pending":
            return idx, item
    return None


def _in_progress_with_child(backlog: list[dict]) -> tuple[int, dict] | None:
    for idx, item in enumerate(backlog):
        if (
            item.get("status") == "in_progress"
            and item.get("assigned_task_id") is not None
        ):
            return idx, item
    return None


async def dispatch_next(parent: Task) -> Task | None:
    """Pick the next backlog item, create a child Task, return it.

    Idempotent: if an item is already ``in_progress`` and points at an
    existing Task, return that Task without inserting a new one. Returns
    ``None`` when the backlog has no pending or assigned-in-progress
    items left.
    """
    async with async_session() as session:
        refreshed_parent = (
            await session.execute(select(Task).where(Task.id == parent.id))
        ).scalar_one()
        backlog = list(refreshed_parent.trio_backlog or [])

        # Recovery path: an item is already in_progress and points at an
        # existing child Task — reuse it.
        existing = _in_progress_with_child(backlog)
        if existing is not None:
            _, item = existing
            child = (
                await session.execute(
                    select(Task).where(Task.id == item["assigned_task_id"]),
                )
            ).scalar_one_or_none()
            if child is not None:
                log.info(
                    "trio.scheduler.reuse_existing_child",
                    parent_id=refreshed_parent.id,
                    child_id=child.id,
                    work_item_id=item.get("id"),
                )
                return child
            # Assigned id points at nothing — fall through to dispatching
            # the next pending item.

        nxt = _next_pending(backlog)
        if nxt is None:
            return None
        idx, item = nxt

        child = Task(
            title=item.get("title") or item.get("description") or "(trio subtask)",
            description=item.get("description") or item.get("title") or "",
            source=refreshed_parent.source,
            status=TaskStatus.QUEUED,
            complexity=TaskComplexity.COMPLEX,
            parent_task_id=refreshed_parent.id,
            freeform_mode=refreshed_parent.freeform_mode,
            repo_id=refreshed_parent.repo_id,
            created_by_user_id=refreshed_parent.created_by_user_id,
            organization_id=refreshed_parent.organization_id,
        )
        session.add(child)
        await session.flush()

        new_item = dict(item)
        new_item["status"] = "in_progress"
        new_item["assigned_task_id"] = child.id
        # JSONB columns require a fresh list reference for SQLAlchemy to
        # mark the column dirty.
        refreshed_parent.trio_backlog = [
            *backlog[:idx],
            new_item,
            *backlog[idx + 1:],
        ]
        await session.commit()

        # Publish task.created so the orchestrator picks up the child. The
        # publisher is wired at process start (Redis in prod, in-memory in
        # tests); a missing publisher must not break dispatch.
        try:
            from shared.events import publish, task_created

            await publish(task_created(child.id))
        except Exception:
            log.warning(
                "trio.scheduler.publish_task_created_failed",
                child_id=child.id,
                exc_info=True,
            )

        log.info(
            "trio.scheduler.dispatched",
            parent_id=refreshed_parent.id,
            child_id=child.id,
            work_item_id=new_item.get("id"),
        )
        return child


async def await_child(parent: Task, child: Task) -> Task:
    """Block until ``child`` reaches a terminal status. Returns refreshed Task.

    Polls the DB every ``_POLL_INTERVAL_S`` seconds. There is no global
    async subscribe seam in :mod:`shared.events` to hook into — events
    are published to a Redis Stream consumed elsewhere — so polling is
    the simplest correct approach here. Cheap: one indexed PK select per
    tick.
    """
    while True:
        async with async_session() as session:
            current = (
                await session.execute(select(Task).where(Task.id == child.id))
            ).scalar_one()
            if current.status in _TERMINAL_STATUSES:
                return current
        await asyncio.sleep(_POLL_INTERVAL_S)
