"""Trio recovery — resume in-flight trio parents on orchestrator startup."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, or_, select

from shared.database import async_session
from shared.events import Event, TaskEventType, publish
from shared.models import ArchitectAttempt, Task, TaskStatus

log = structlog.get_logger()


async def _run_trio_parent_logged(parent_task: Task) -> None:
    """Run the idempotent trio driver, logging any unobserved exception with a
    full traceback.

    Fire-and-forget ``asyncio.create_task`` otherwise swallows failures
    silently — that bit us on task 170 after the ADR-013 deploy. structlog's
    ``log.exception`` doesn't capture ``sys.exc_info`` reliably under our
    processor chain, so we format the traceback ourselves.
    """
    from agent.lifecycle.trio import run_trio_parent

    try:
        await run_trio_parent(parent_task)
    except Exception as exc:
        import traceback

        log.error(
            "trio.run_failed",
            parent_id=parent_task.id,
            error=str(exc),
            error_type=type(exc).__name__,
            traceback=traceback.format_exc(),
        )


def _resumable_trio_where():
    """Trio parents whose idempotent driver should be re-invoked.

    In scope (same set boot recovery and the watchdog share):
      - ``TRIO_EXECUTING``: actively running the per-item loop.
      - ``ARCHITECT_BACKLOG_EMIT``: backlog emitted but the per-item builder
        loop never started (observed on the 2026-05-23 harpoon run).
      - ``AWAITING_DESIGN_APPROVAL`` with ``freeform_mode=True``: the standin
        needs the driver re-invoked so the design gate fires. A non-freeform
        design gate is a real human wait — deliberately excluded.
    Re-invoking is safe: ``run_trio_parent`` checks committed artefacts first.
    """
    return or_(
        Task.status == TaskStatus.TRIO_EXECUTING,
        Task.status == TaskStatus.ARCHITECT_BACKLOG_EMIT,
        (Task.status == TaskStatus.AWAITING_DESIGN_APPROVAL) & (Task.freeform_mode.is_(True)),
    )


async def resume_all_trio_parents() -> None:
    """Find every task in TRIO_EXECUTING and dispatch run_trio_parent for it.

    Called once at orchestrator startup. The orchestrator + architect modules
    enforce idempotency internally, so re-running them on tasks that already
    have committed work is safe.
    """
    # Resume every trio parent that's actively being driven OR parked at
    # one of the front-half gates that can deadlock silently. In scope:
    #   - ``TRIO_EXECUTING``: actively running the per-item loop.
    #   - ``AWAITING_DESIGN_APPROVAL`` with ``freeform_mode=True``: the
    #     standin needs the driver re-invoked so case B of
    #     ``_advance_through_design_gate`` fires (writes the verdict,
    #     transitions to ARCHITECT_BACKLOG_EMIT, runs run_initial).
    #   - ``ARCHITECT_BACKLOG_EMIT`` (any backlog size): observed on the
    #     2026-05-23 harpoon run that children stranded here with
    #     ``trio_phase=AWAITING_BUILDER`` — backlog was emitted but the
    #     per-item builder loop never started. Re-invoking run_trio_parent
    #     is idempotent: ``has_backlog=True`` short-circuits the
    #     architect re-run and falls into the per-item loop; empty backlog
    #     re-runs ``architect.run_initial`` to emit a fresh one.
    async with async_session() as s:
        rows = (await s.execute(select(Task).where(_resumable_trio_where()))).scalars().all()

    if rows:
        log.info(
            "trio.recovery.resuming",
            count=len(rows),
            task_ids=[r.id for r in rows],
        )
        for parent in rows:
            asyncio.create_task(_run_trio_parent_logged(parent))  # noqa: RUF006

    # Freeform AWAITING_REVIEW recovery (2026-05-23). Tasks parked at
    # AWAITING_REVIEW in a freeform repo were never getting auto-merged
    # because the standin wasn't wired at the gate. The wiring landed in
    # _open_integration_pr_and_transition, but already-stuck tasks would
    # remain there forever (the transition won't re-fire). Sweep them on
    # startup and run the standin so they get auto-merged + DONE +
    # trigger the scaffold serial-dispatch fan-in.
    async with async_session() as s:
        stuck_review = (
            (
                await s.execute(
                    select(Task).where(
                        (Task.status == TaskStatus.AWAITING_REVIEW)
                        & (Task.freeform_mode.is_(True))
                        & (Task.pr_url.is_not(None))
                    )
                )
            )
            .scalars()
            .all()
        )

    if stuck_review:
        from agent.lifecycle.trio import (
            _try_freeform_pr_review_standin,
        )
        from agent.lifecycle.trio.architect import _prepare_parent_workspace

        log.info(
            "trio.recovery.freeform_review_sweep",
            count=len(stuck_review),
            task_ids=[t.id for t in stuck_review],
        )

        async def _resume_pr_review(parent_task: Task) -> None:
            try:
                workspace = await _prepare_parent_workspace(parent_task)
                workspace_root = workspace.root if hasattr(workspace, "root") else str(workspace)
                await _try_freeform_pr_review_standin(
                    parent=parent_task,
                    workspace_root=workspace_root,
                    pr_url=parent_task.pr_url or "",
                )
            except Exception as exc:
                import traceback

                log.error(
                    "trio.recovery.freeform_review_failed",
                    parent_id=parent_task.id,
                    error=str(exc),
                    traceback=traceback.format_exc(),
                )

        for parent in stuck_review:
            asyncio.create_task(_resume_pr_review(parent))  # noqa: RUF006

    # AWAITING_CLARIFICATION + trio_phase set. The architect is
    # paused; if the answer landed pre-crash we re-publish RESOLVED so
    # on_architect_clarification_resolved transitions state and calls
    # architect.resume. If no answer yet, do nothing — we're still
    # waiting on a human / PO.
    async with async_session() as s:
        awaiting = (
            (
                await s.execute(
                    select(Task)
                    .where(Task.status == TaskStatus.AWAITING_CLARIFICATION)
                    .where(Task.trio_phase.is_not(None))
                )
            )
            .scalars()
            .all()
        )
        for task in awaiting:
            latest = (
                await s.execute(
                    select(ArchitectAttempt)
                    .where(ArchitectAttempt.task_id == task.id)
                    .where(ArchitectAttempt.clarification_question.is_not(None))
                    .order_by(ArchitectAttempt.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if latest is None:
                continue
            if latest.clarification_answer is None:
                log.info(
                    "trio.recovery.awaiting_clarification.still_waiting",
                    task_id=task.id,
                )
                continue
            log.info(
                "trio.recovery.awaiting_clarification.republish_resolved",
                task_id=task.id,
            )
            await publish(
                Event(
                    type=TaskEventType.ARCHITECT_CLARIFICATION_RESOLVED,
                    task_id=task.id,
                )
            )


# ---------------------------------------------------------------------------
# Continuous watchdog — trio's missing sibling of scaffold_heartbeat_watchdog.
# Boot recovery (above) only runs at startup, so a parent that stalled mid-run
# — or a freeform task whose design-gate standin never fired — sat forever
# holding its repo until a human deleted it. This re-invokes the idempotent
# driver every interval for parents stale past the threshold.
# ---------------------------------------------------------------------------

_TRIO_WATCHDOG_INTERVAL_SECS = 5 * 60
# Mirror scaffold's threshold: a trio parent legitimately sits a long time
# between item builds / architect LLM calls without bumping ``updated_at``, so
# a generous window avoids re-firing a still-progressing driver.
_TRIO_STALL_THRESHOLD = timedelta(minutes=90)


async def _notify_stall_recovery(task: Task) -> None:
    """Tell the owner their task stalled and is being auto-recovered.

    The whole point of the watchdog is that a silent strand never again needs a
    manual delete to even be noticed. Best-effort: a notifier outage must never
    block the recovery itself.
    """
    try:
        from shared.notifier import send_telegram_async

        minutes = int(_TRIO_STALL_THRESHOLD.total_seconds() // 60)
        await send_telegram_async(
            f"⚠️ Task #{task.id} stalled in {task.status.value} for "
            f">{minutes} min — auto-recovering.",
            task_id=task.id,
        )
    except Exception:
        log.warning("trio.watchdog.notify_failed", task_id=task.id, exc_info=True)


async def resume_stalled_trio_parents_once() -> list[int]:
    """One watchdog tick. Re-invoke the driver for stale trio parents; notify
    the owner of each. Returns the resumed task ids (empty when nothing
    stalled) so the loop and tests can observe progress.
    """
    cutoff = datetime.now(UTC) - _TRIO_STALL_THRESHOLD
    async with async_session() as s:
        rows = (
            (
                await s.execute(
                    select(Task).where(and_(Task.updated_at < cutoff, _resumable_trio_where()))
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        return []

    log.warning(
        "trio.watchdog.stalled_detected",
        count=len(rows),
        task_ids=[r.id for r in rows],
    )
    for parent in rows:
        await _notify_stall_recovery(parent)
        asyncio.create_task(_run_trio_parent_logged(parent))  # noqa: RUF006
    return [r.id for r in rows]


async def trio_heartbeat_watchdog() -> None:
    """Continuous sibling of ``scaffold_heartbeat_watchdog`` for trio parents.

    Background task started from run.py lifespan. Runs forever; one tick every
    ``_TRIO_WATCHDOG_INTERVAL_SECS``.
    """
    log.info("trio.watchdog.started", interval_secs=_TRIO_WATCHDOG_INTERVAL_SECS)
    while True:
        try:
            await asyncio.sleep(_TRIO_WATCHDOG_INTERVAL_SECS)
            await resume_stalled_trio_parents_once()
        except asyncio.CancelledError:
            log.info("trio.watchdog.cancelled")
            raise
        except Exception:
            log.exception("trio.watchdog.tick_failed")
            continue
