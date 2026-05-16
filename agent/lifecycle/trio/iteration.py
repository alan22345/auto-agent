"""Trio iteration phase entry point — ADR-017.

User feedback on a complex_large task whose integration PR is open
(``AWAITING_REVIEW``) lands here. The handler transitions the task to
``ITERATING`` and re-enters :func:`run_trio_parent` with an
``iteration_context`` carrying the feedback and PR URL.

Re-entrant feedback (a second message arriving while ITERATING is still
running) does NOT start a second dispatch. Instead the message is pushed
onto the per-task guidance channel; the running architect / builder
picks it up between turns. Mirrors the ``_active_clarification_tasks``
pattern in :mod:`agent.lifecycle.conversation`.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from agent.lifecycle.trio import run_trio_parent
from orchestrator.state_machine import transition
from shared.database import async_session
from shared.models import Task, TaskStatus
from shared.task_channel import task_channel

log = logging.getLogger(__name__)

# Module-level set of task IDs currently in the iteration loop. Guards
# against re-entrant dispatch when feedback arrives while a previous
# iteration is still running.
_active_iteration_tasks: set[int] = set()


async def handle_iteration_feedback(task_id: int, message: str) -> None:
    """ADR-017 entry — user feedback on a complex_large task's PR.

    No-op if the task isn't in AWAITING_REVIEW or ITERATING (the gate
    guards against stray messages — e.g. a late thread reply on a
    task that was just merged).
    """
    if task_id in _active_iteration_tasks:
        log.info(
            "iteration.busy.push_guidance",
            extra={"task_id": task_id, "message_preview": message[:80]},
        )
        await task_channel(task_id).push_guidance(message)
        return

    async with async_session() as s:
        task = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
        if task is None:
            return
        # Refetch-and-bail: if the merge webhook already won, do nothing.
        if task.status == TaskStatus.DONE:
            return
        if task.status not in (TaskStatus.AWAITING_REVIEW, TaskStatus.ITERATING):
            log.info(
                "iteration.wrong_status.dropped",
                extra={"task_id": task_id, "status": task.status.value},
            )
            return
        # Transition to ITERATING (no-op if already there — re-entrant guard
        # at top of function handles the parallel-message case).
        if task.status == TaskStatus.AWAITING_REVIEW:
            await transition(
                s,
                task,
                TaskStatus.ITERATING,
                message="trio: iterating on user feedback",
            )
            await s.commit()
        pr_url = task.pr_url or ""

    iteration_context = {"feedback": message, "pr_url": pr_url}
    _active_iteration_tasks.add(task_id)
    try:
        async with async_session() as s:
            parent = (await s.execute(select(Task).where(Task.id == task_id))).scalar_one()
        await run_trio_parent(parent, iteration_context=iteration_context)
    finally:
        _active_iteration_tasks.discard(task_id)
