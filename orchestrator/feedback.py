"""Learning/feedback loop — tracks PR outcomes and surfaces patterns."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Task, TaskOutcome
from shared.types import FeedbackSummary

log = logging.getLogger(__name__)


async def record_outcome(
    session: AsyncSession,
    task_id: int,
    pr_approved: bool,
    review_rounds: int = 0,
    feedback_summary: str = "",
) -> TaskOutcome:
    """Record the outcome of a completed task's PR."""
    task = await session.get(Task, task_id)
    if not task:
        raise ValueError(f"Task {task_id} not found")

    time_to_complete = None
    if task.created_at:
        delta = datetime.now(timezone.utc) - task.created_at
        time_to_complete = delta.total_seconds()

    result = await session.execute(
        select(TaskOutcome).where(TaskOutcome.task_id == task_id)
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.pr_approved = pr_approved
        existing.review_rounds = review_rounds
        existing.time_to_complete_seconds = time_to_complete
        existing.feedback_summary = feedback_summary
        outcome = existing
    else:
        outcome = TaskOutcome(
            task_id=task_id,
            pr_approved=pr_approved,
            review_rounds=review_rounds,
            time_to_complete_seconds=time_to_complete,
            feedback_summary=feedback_summary,
        )
        session.add(outcome)

    await session.flush()
    return outcome


async def get_recent_outcomes(
    session: AsyncSession,
    limit: int = 20,
    *,
    organization_id: int | None = None,
) -> list[TaskOutcome]:
    q = select(TaskOutcome).order_by(TaskOutcome.created_at.desc()).limit(limit)
    if organization_id is not None:
        q = q.join(Task, TaskOutcome.task_id == Task.id).where(
            Task.organization_id == organization_id,
        )
    result = await session.execute(q)
    return list(result.scalars().all())


async def get_feedback_summary(
    session: AsyncSession,
    *,
    organization_id: int | None = None,
) -> FeedbackSummary:
    q = select(
        func.count(TaskOutcome.id),
        func.sum(func.cast(TaskOutcome.pr_approved == True, int)),
        func.avg(TaskOutcome.review_rounds),
    )
    if organization_id is not None:
        q = q.select_from(TaskOutcome).join(
            Task, TaskOutcome.task_id == Task.id,
        ).where(Task.organization_id == organization_id)
    result = await session.execute(q)
    row = result.one()
    total: int = row[0] or 0
    approved: int = row[1] or 0

    return FeedbackSummary(
        total_outcomes=total,
        approved=approved,
        rejected=total - approved,
        approval_rate=round(approved / total * 100, 1) if total > 0 else 0.0,
        avg_review_rounds=round(float(row[2] or 0), 1),
    )


async def analyze_patterns(
    session: AsyncSession,
    *,
    organization_id: int | None = None,
) -> str:
    """Return a text summary of recent PR outcomes for the /feedback/patterns endpoint."""
    outcomes = await get_recent_outcomes(
        session, limit=20, organization_id=organization_id,
    )
    if len(outcomes) < 3:
        return "Not enough data yet (need at least 3 completed tasks)."

    lines: list[str] = []
    for o in outcomes:
        task = await session.get(Task, o.task_id)
        title = task.title if task else f"Task #{o.task_id}"
        status = "APPROVED" if o.pr_approved else "REJECTED"
        line = f"- [{status}] {title} | Review rounds: {o.review_rounds}"
        if o.feedback_summary:
            line += f" | {o.feedback_summary[:200]}"
        lines.append(line)

    return "\n".join(lines)


def build_learning_context(outcomes: list[TaskOutcome]) -> str:
    """Build a context string from recent rejected outcomes to inject into Claude Code prompts."""
    if not outcomes:
        return ""

    rejected = [o for o in outcomes if not o.pr_approved and o.feedback_summary]
    if not rejected:
        return ""

    lines = ["## Lessons from recent PRs (avoid these patterns):"]
    for o in rejected[:5]:
        lines.append(f"- {o.feedback_summary[:200]}")

    return "\n".join(lines)
