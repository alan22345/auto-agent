"""Scaffold recovery — resume in-flight scaffold parents on orchestrator startup.

Mirrors ``agent.lifecycle.trio.recovery`` for SCAFFOLD parents (ADR-018).
Re-invokes ``run_scaffold_parent`` for any task parked at one of the
scaffold AWAITING_* / BUILDING_* gates so a container restart doesn't
strand them. The driver is idempotent: phases reload from disk, gate
verdicts use ``ADD VALUE IF NOT EXISTS`` style writes, so re-running on
a task that has already progressed is safe.

This module also exposes a heartbeat watchdog
(``scaffold_heartbeat_watchdog``) that periodically scans for SCAFFOLD
parents stuck in a BUILDING_* status with no ``updated_at`` movement
for N minutes, and re-invokes the driver for them. This catches the
silent-stall failure mode where the in-process agent dies without
raising — e.g. the 2026-05-22 ARG_MAX freeze where a 112KB grill
summary blew through the kernel argv limit and uvloop blocked instead
of raising.
"""

from __future__ import annotations

import asyncio
import traceback
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select

from shared.database import async_session
from shared.models import Task, TaskStatus

log = structlog.get_logger()


# Every non-terminal scaffold-parent status — the driver's `while True`
# loop knows how to advance each one.
_SCAFFOLD_RESUMABLE_STATUSES = (
    TaskStatus.AWAITING_INTENT_GRILL,
    TaskStatus.BUILDING_ROOT_ADR,
    TaskStatus.AWAITING_ROOT_ADR_APPROVAL,
    TaskStatus.BUILDING_DOMAIN_ADRS,
    TaskStatus.AWAITING_DOMAIN_GRILL,
    TaskStatus.AWAITING_DOMAIN_ADR_APPROVAL,
    TaskStatus.AWAITING_REQUIRED_SECRETS,
    TaskStatus.DISPATCHING_DOMAIN_BUILDS,
    TaskStatus.BUILDING_DOMAINS,
    TaskStatus.AWAITING_FINAL_VERIFICATION,
)


async def resume_all_scaffold_parents() -> None:
    """Find every SCAFFOLD parent in a non-terminal status and dispatch the driver.

    Called once at orchestrator startup, alongside ``resume_all_trio_parents``.
    """

    from agent.lifecycle.scaffold import run_scaffold_parent
    from shared.models import TaskComplexity

    async with async_session() as s:
        rows = (
            (
                await s.execute(
                    select(Task).where(
                        Task.complexity == TaskComplexity.SCAFFOLD,
                        Task.status.in_(_SCAFFOLD_RESUMABLE_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        return

    log.info(
        "scaffold.recovery.resuming",
        count=len(rows),
        task_ids=[r.id for r in rows],
    )

    # Touch ``updated_at`` on every recovered task BEFORE handing off to the
    # driver. The watchdog uses ``updated_at`` to detect stalls; without this
    # bump, a freshly-resumed task with a multi-hour-old ``updated_at`` (from
    # whenever the last in-process transition fired) looks "stalled" on the
    # very next watchdog tick — even though the driver is actively making
    # LLM calls. That false positive caused two concurrent drivers to race
    # on transitions and trip InvalidTransition errors.
    from sqlalchemy import update as sa_update
    async with async_session() as s:
        await s.execute(
            sa_update(Task)
            .where(Task.id.in_([r.id for r in rows]))
            .values(updated_at=datetime.now(UTC))
        )
        await s.commit()

    async def _run_with_exception_logging(parent_task: Task) -> None:
        try:
            await run_scaffold_parent(parent_task)
        except Exception as exc:
            log.error(
                "scaffold.recovery.parent_failed",
                task_id=parent_task.id,
                error=str(exc),
                traceback=traceback.format_exc(),
            )

    for row in rows:
        asyncio.create_task(_run_with_exception_logging(row))  # noqa: RUF006


# ---------------------------------------------------------------------------
# Heartbeat watchdog — detect silent stalls and re-invoke the driver.
# ---------------------------------------------------------------------------


# Only the BUILDING_* / processing statuses are "active" — i.e. the driver
# should be making progress. AWAITING_* statuses (root ADR approval,
# domain grill, etc.) are legitimately paused while a human or PO standin
# acts, so they don't qualify as stalls. AWAITING_INTENT_GRILL IS active
# (the agent is running) so we include it.
_ACTIVE_STATUSES = (
    TaskStatus.AWAITING_INTENT_GRILL,
    TaskStatus.BUILDING_ROOT_ADR,
    TaskStatus.BUILDING_DOMAIN_ADRS,
    TaskStatus.DISPATCHING_DOMAIN_BUILDS,
    TaskStatus.AWAITING_FINAL_VERIFICATION,
)

_HEARTBEAT_INTERVAL_SECS = 5 * 60          # poll every 5 minutes
# BUILDING_DOMAIN_ADRS does NOT bump ``updated_at`` between domains — a 7-domain
# scaffold can legitimately sit in that status for 50+ min with no row update.
# 90 min keeps the watchdog catching genuine multi-hour hangs (the original
# 12-hour ARG_MAX freeze) without false-positive re-invoking a still-working
# driver. Lower this only after BUILDING_DOMAIN_ADRS gets per-domain heartbeat
# updates.
_STALL_THRESHOLD = timedelta(minutes=90)


async def scaffold_heartbeat_watchdog() -> None:
    """Periodically detect stalled SCAFFOLD parents and re-invoke the driver.

    Background task started from run.py lifespan, alongside the other
    pollers. Runs forever; one tick every ``_HEARTBEAT_INTERVAL_SECS``.

    A task is "stalled" when:
      - complexity == SCAFFOLD
      - status is one of the actively-processing statuses
      - updated_at is older than _STALL_THRESHOLD

    The driver is idempotent (phase artefacts on disk are checked before
    re-running LLM work, gate verdicts use idempotent writes), so a
    re-invocation while the original task is still hung is safe: the
    original eventually raises or finishes; the new one observes the
    current state and proceeds.
    """

    from agent.lifecycle.scaffold import run_scaffold_parent
    from shared.models import TaskComplexity

    log.info("scaffold.heartbeat.started", interval_secs=_HEARTBEAT_INTERVAL_SECS)

    while True:
        try:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECS)
            cutoff = datetime.now(UTC) - _STALL_THRESHOLD
            async with async_session() as s:
                rows = (
                    (
                        await s.execute(
                            select(Task).where(
                                Task.complexity == TaskComplexity.SCAFFOLD,
                                Task.status.in_(_ACTIVE_STATUSES),
                                Task.updated_at < cutoff,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

            if not rows:
                continue

            log.warning(
                "scaffold.heartbeat.stalled_parents_detected",
                count=len(rows),
                task_ids=[r.id for r in rows],
            )

            for row in rows:
                async def _runner(parent_task: Task = row) -> None:
                    try:
                        await run_scaffold_parent(parent_task)
                    except Exception as exc:
                        log.error(
                            "scaffold.heartbeat.reinvoke_failed",
                            task_id=parent_task.id,
                            error=str(exc),
                            traceback=traceback.format_exc(),
                        )

                asyncio.create_task(_runner())  # noqa: RUF006

        except asyncio.CancelledError:
            log.info("scaffold.heartbeat.cancelled")
            raise
        except Exception:
            # Don't let a transient DB blip kill the watchdog forever.
            log.exception("scaffold.heartbeat.tick_failed")
            # Continue to the next iteration after the sleep.
            continue
