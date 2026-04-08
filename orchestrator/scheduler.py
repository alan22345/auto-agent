"""Cron scheduler for recurring tasks (dependency updates, security scans, etc.)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import async_session
from shared.events import Event
from shared.models import ScheduledTask, Task, TaskSource
from shared.redis_client import get_redis, publish_event

log = logging.getLogger(__name__)

CHECK_INTERVAL = 60  # Check every 60 seconds


async def run_scheduler() -> None:
    """Main scheduler loop — checks for due scheduled tasks and creates them."""
    log.info("Scheduler started")

    while True:
        try:
            async with async_session() as session:
                await _check_and_run(session)
        except Exception:
            log.exception("Scheduler error")
        await asyncio.sleep(CHECK_INTERVAL)


async def _check_and_run(session: AsyncSession) -> None:
    """Check all enabled scheduled tasks and run any that are due."""
    result = await session.execute(
        select(ScheduledTask).where(ScheduledTask.enabled == True)
    )
    schedules = result.scalars().all()
    now = datetime.now(timezone.utc)

    for schedule in schedules:
        if _is_due(schedule, now):
            log.info(f"Scheduled task '{schedule.name}' is due, creating task")
            await _create_scheduled_task(session, schedule)
            schedule.last_run_at = now
            await session.commit()


def _is_due(schedule: ScheduledTask, now: datetime) -> bool:
    """Check if a scheduled task should run now."""
    base_time = schedule.last_run_at or schedule.created_at or now
    # Make base_time timezone-aware if it isn't
    if base_time.tzinfo is None:
        base_time = base_time.replace(tzinfo=timezone.utc)
    cron = croniter(schedule.cron_expression, base_time)
    next_run = cron.get_next(datetime)
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=timezone.utc)
    return now >= next_run


async def _create_scheduled_task(session: AsyncSession, schedule: ScheduledTask) -> None:
    """Create a task from a scheduled task definition."""
    task = Task(
        title=f"[Scheduled] {schedule.task_title}",
        description=schedule.task_description,
        source=TaskSource.MANUAL,
        source_id=f"scheduled:{schedule.name}",
    )
    session.add(task)
    await session.flush()

    r = await get_redis()
    event = Event(type="task.created", task_id=task.id)
    await publish_event(r, event.to_redis())
    await r.aclose()
