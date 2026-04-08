"""Metrics queries and dashboard endpoint."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import get_session
from shared.models import Task, TaskComplexity, TaskHistory, TaskOutcome, TaskStatus
from shared.types import (
    MetricsResponse,
    PROutcomeMetrics,
    TaskMetricsResponse,
    TimelineEntry,
)

router = APIRouter()


@router.get("/metrics", response_model=MetricsResponse)
async def get_metrics(
    days: int = 30,
    session: AsyncSession = Depends(get_session),
) -> MetricsResponse:
    """Return task metrics for the dashboard."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Total tasks
    total_result = await session.execute(
        select(func.count(Task.id)).where(Task.created_at >= since)
    )
    total_tasks = total_result.scalar_one()

    # Tasks by status
    status_result = await session.execute(
        select(Task.status, func.count(Task.id))
        .where(Task.created_at >= since)
        .group_by(Task.status)
    )
    by_status: dict[str, int] = {row[0].value: row[1] for row in status_result.all()}

    # Tasks by complexity
    complexity_result = await session.execute(
        select(Task.complexity, func.count(Task.id))
        .where(Task.created_at >= since, Task.complexity.isnot(None))
        .group_by(Task.complexity)
    )
    by_complexity: dict[str, int] = {row[0].value: row[1] for row in complexity_result.all()}

    # Tasks by source
    source_result = await session.execute(
        select(Task.source, func.count(Task.id))
        .where(Task.created_at >= since)
        .group_by(Task.source)
    )
    by_source: dict[str, int] = {row[0].value: row[1] for row in source_result.all()}

    # Success/failure rates
    completed = by_status.get("done", 0)
    failed = by_status.get("failed", 0)
    finished = completed + failed
    success_rate = (completed / finished * 100) if finished > 0 else 0.0

    # Average time to complete (from INTAKE to DONE)
    avg_duration = await _avg_completion_time(session, since)

    # PR outcome stats (from learning loop)
    pr_outcomes = await _get_pr_outcome_metrics(session, since)

    # Active tasks right now
    active_result = await session.execute(
        select(func.count(Task.id)).where(
            Task.status.notin_([TaskStatus.DONE, TaskStatus.FAILED])
        )
    )
    active_tasks = active_result.scalar_one()

    return MetricsResponse(
        period_days=days,
        total_tasks=total_tasks,
        active_tasks=active_tasks,
        success_rate_pct=round(success_rate, 1),
        by_status=by_status,
        by_complexity=by_complexity,
        by_source=by_source,
        avg_duration_hours=round(avg_duration / 3600, 1) if avg_duration else None,
        pr_outcomes=pr_outcomes,
    )


async def _get_pr_outcome_metrics(session: AsyncSession, since: datetime) -> PROutcomeMetrics:
    """Get PR outcome metrics for the period."""
    outcome_result = await session.execute(
        select(
            func.count(TaskOutcome.id),
            func.sum(case((TaskOutcome.pr_approved == True, 1), else_=0)),
            func.sum(case((TaskOutcome.pr_approved == False, 1), else_=0)),
            func.avg(TaskOutcome.review_rounds),
            func.avg(TaskOutcome.time_to_complete_seconds),
        ).where(TaskOutcome.created_at >= since)
    )
    row = outcome_result.one()
    total_outcomes: int = row[0] or 0
    approved_count: int = row[1] or 0
    rejected_count: int = row[2] or 0
    avg_review_rounds: float = round(float(row[3] or 0), 1)
    avg_completion_seconds: float | None = round(float(row[4]), 0) if row[4] else None

    return PROutcomeMetrics(
        total=total_outcomes,
        approved=approved_count,
        rejected=rejected_count,
        approval_rate_pct=round(approved_count / total_outcomes * 100, 1) if total_outcomes > 0 else 0.0,
        avg_review_rounds=avg_review_rounds,
        avg_completion_seconds=avg_completion_seconds,
    )


async def _avg_completion_time(session: AsyncSession, since: datetime) -> float | None:
    """Calculate average seconds from INTAKE to DONE."""
    done_tasks = await session.execute(
        select(Task.id, Task.created_at).where(
            Task.status == TaskStatus.DONE,
            Task.created_at >= since,
        )
    )

    total_seconds = 0.0
    count = 0
    for task_id, created_at in done_tasks.all():
        done_entry = await session.execute(
            select(TaskHistory.created_at).where(
                TaskHistory.task_id == task_id,
                TaskHistory.to_status == TaskStatus.DONE,
            ).order_by(TaskHistory.created_at.desc()).limit(1)
        )
        done_at = done_entry.scalar_one_or_none()
        if done_at and created_at:
            delta = (done_at - created_at).total_seconds()
            total_seconds += delta
            count += 1

    return total_seconds / count if count > 0 else None


@router.get("/metrics/tasks/{task_id}", response_model=TaskMetricsResponse)
async def get_task_metrics(task_id: int, session: AsyncSession = Depends(get_session)) -> TaskMetricsResponse:
    """Get detailed metrics for a specific task including its timeline."""
    history_result = await session.execute(
        select(TaskHistory)
        .where(TaskHistory.task_id == task_id)
        .order_by(TaskHistory.created_at.asc())
    )
    history = history_result.scalars().all()

    timeline: list[TimelineEntry] = []
    for h in history:
        timeline.append(TimelineEntry.model_validate({
            "from": h.from_status.value if h.from_status else None,
            "to": h.to_status.value,
            "message": h.message,
            "timestamp": h.created_at.isoformat() if h.created_at else None,
        }))

    # Time spent in each status
    durations: dict[str, float] = {}
    for i, entry in enumerate(history):
        status = entry.to_status.value
        if i + 1 < len(history) and entry.created_at and history[i + 1].created_at:
            seconds = (history[i + 1].created_at - entry.created_at).total_seconds()
            durations[status] = durations.get(status, 0) + seconds

    return TaskMetricsResponse(
        task_id=task_id,
        timeline=timeline,
        time_in_status_seconds=durations,
    )
