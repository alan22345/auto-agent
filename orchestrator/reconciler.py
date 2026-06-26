"""DB-truth reconciler — the backstop that guarantees no task strands silently.

The event bus is best-effort: ``publish`` is a bare ``XADD`` after the DB commit
(a Redis blip drops the event), and the consumer acks in a ``finally`` (a handler
exception drops the event). Either way a task can stop advancing with no error
surfaced — the failure mode behind "I only noticed the stuck task because I went
to delete it".

Rather than make the bus reliable (outbox + redelivery + a handler idempotency
audit), this treats the database as the source of truth. Every interval it
sweeps non-terminal tasks and flags any that has gone quiet past a threshold and
is not already being re-driven by a dedicated loop. A flagged task notifies its
owner, so a silent strand is always surfaced.

This is the consolidation seam for the family of ad-hoc recovery loops
(``task_timeout_watchdog``, the scaffold/trio heartbeat watchdogs, the CI/PR
pollers, boot ``_recover_stuck_tasks``): those actuate the states they own, this
backstops everything they don't. The decision is a pure function so it can be
exhaustively unit-tested without a database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from shared.database import async_session
from shared.models import Task, TaskStatus

log = structlog.get_logger()

# A last-resort backstop, not a competitor to the specialised loops: the
# threshold sits ABOVE every one of them (scaffold/trio watchdogs fire at 90
# min, the timeout watchdog hard-fails CODING at 2 h). When any of those
# re-drives a task it transitions state and bumps ``updated_at``, resetting the
# staleness clock — so a task only ever reaches this backstop when every loop
# that should have moved it has already failed to. That single fact removes the
# need to enumerate which loop owns which trio/scaffold sub-state.
RECONCILE_STALL_THRESHOLD = timedelta(hours=3)
RECONCILE_INTERVAL_SECS = 10 * 60

# Tasks reaching these have stopped; they hold no work to reconcile.
_TERMINAL = {TaskStatus.DONE, TaskStatus.FAILED}

# QUEUED waits for a free slot without bumping ``updated_at`` (it isn't stuck,
# just queued), and PLANNING/CODING are the timeout watchdog's to fail — so the
# backstop stays quiet on all three to avoid false alarms / double-reporting.
_REDRIVEN_STATUSES = {
    TaskStatus.QUEUED,
    TaskStatus.PLANNING,
    TaskStatus.CODING,
}


def is_silently_stuck(
    task: Task,
    *,
    now: datetime,
    heartbeat_alive: bool,
    threshold: timedelta = RECONCILE_STALL_THRESHOLD,
) -> bool:
    """True if *task* has gone quiet past *threshold* with nothing driving it.

    Pure: depends only on the task's status, ``updated_at``, and whether an
    agent loop is currently heart-beating for it. A live heartbeat always wins
    (a slow-but-working agent is never flagged), so this can never fire on a
    task that is merely taking a long time.
    """
    if task.status in _TERMINAL or task.status in _REDRIVEN_STATUSES:
        return False
    if heartbeat_alive:
        return False

    updated = task.updated_at
    if updated is None:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return (now - updated) > threshold


# Remember what we've already surfaced so a parked task is reported once, not
# every tick. Keyed by (task_id, updated_at) so a task that progresses and then
# re-stalls is surfaced afresh.
_surfaced: set[tuple[int, str]] = set()


async def _surface_stuck(task: Task) -> None:
    """Notify the owner once that a task is silently stuck. Best-effort — a
    notifier outage must never break the sweep."""
    key = (task.id, str(task.updated_at))
    if key in _surfaced:
        return
    _surfaced.add(key)
    log.warning(
        "reconciler.silently_stuck",
        task_id=task.id,
        status=task.status.value,
        updated_at=str(task.updated_at),
    )
    try:
        from shared.notifier import send_telegram_async

        minutes = int(RECONCILE_STALL_THRESHOLD.total_seconds() // 60)
        await send_telegram_async(
            f"⚠️ Task #{task.id} has been stuck in {task.status.value} for "
            f">{minutes} min with nothing driving it. It needs a look.",
            task_id=task.id,
        )
    except Exception:
        log.warning("reconciler.notify_failed", task_id=task.id, exc_info=True)


async def reconcile_once() -> list[int]:
    """One sweep. Surface every silently-stuck task; return their ids."""
    from shared.task_channel import task_channel

    now = datetime.now(UTC)
    async with async_session() as s:
        rows = (await s.execute(select(Task).where(Task.status.notin_(_TERMINAL)))).scalars().all()

    stuck: list[int] = []
    for task in rows:
        alive = await task_channel(task.id).is_alive()
        if is_silently_stuck(task, now=now, heartbeat_alive=alive):
            stuck.append(task.id)
            await _surface_stuck(task)

    if stuck:
        log.warning("reconciler.sweep", stuck_count=len(stuck), task_ids=stuck)
    return stuck


async def reconciler_loop() -> None:
    """Periodic DB-truth backstop. Runs forever; one sweep per interval."""
    log.info("reconciler.started", interval_secs=RECONCILE_INTERVAL_SECS)
    while True:
        try:
            await asyncio.sleep(RECONCILE_INTERVAL_SECS)
            await reconcile_once()
        except asyncio.CancelledError:
            log.info("reconciler.cancelled")
            raise
        except Exception:
            log.exception("reconciler.tick_failed")
            continue
