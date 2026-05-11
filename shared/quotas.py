"""Per-org quota lookups. Pure functions over an AsyncSession.

All time windows are UTC days (00:00:00-23:59:59.999999) -- switch to per-org
TZ later if customers ask for it. v1 keeps it predictable.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from shared.models import Organization, Plan, Task, TaskStatus, UsageEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# N818: intentionally not named QuotaExceededError -- the shorter name is the
# public contract used by router (HTTP 429) and the agent loop (BLOCKED_ON_QUOTA).
class QuotaExceeded(Exception):  # noqa: N818
    """Per-org quota violation. Surfaced as HTTP 429 by the router, or used
    to trigger BLOCKED_ON_QUOTA transitions inside the agent loop."""


# Mirrors orchestrator.queue.ACTIVE_STATUSES but kept local to avoid a
# cross-layer import. Update both together.
_ACTIVE_STATUSES = {
    TaskStatus.PLANNING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.AWAITING_CLARIFICATION,
    TaskStatus.CODING,
    TaskStatus.PR_CREATED,
    TaskStatus.AWAITING_CI,
    TaskStatus.AWAITING_REVIEW,
    TaskStatus.BLOCKED,
}


def _utc_day_bounds(now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    now = now or dt.datetime.now(dt.UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    return start, end


async def get_plan_for_org(session: AsyncSession, org_id: int) -> Plan:
    """Return the Plan attached to this org. Raises LookupError if missing."""
    q = await session.execute(
        select(Plan).join(Organization, Organization.plan_id == Plan.id).where(
            Organization.id == org_id
        )
    )
    plan = q.scalar_one_or_none()
    if plan is None:
        raise LookupError(f"No plan attached to org {org_id}")
    return plan


async def count_active_tasks_for_org(session: AsyncSession, org_id: int) -> int:
    q = await session.execute(
        select(func.count(Task.id)).where(
            Task.organization_id == org_id,
            Task.status.in_(_ACTIVE_STATUSES),
        )
    )
    return q.scalar_one()


async def count_tasks_created_today(session: AsyncSession, org_id: int) -> int:
    start, end = _utc_day_bounds()
    q = await session.execute(
        select(func.count(Task.id)).where(
            Task.organization_id == org_id,
            Task.created_at >= start,
            Task.created_at < end,
        )
    )
    return q.scalar_one()


async def sum_tokens_today(session: AsyncSession, org_id: int) -> tuple[int, int]:
    start, end = _utc_day_bounds()
    q = await session.execute(
        select(
            func.coalesce(func.sum(UsageEvent.input_tokens), 0),
            func.coalesce(func.sum(UsageEvent.output_tokens), 0),
        ).where(
            UsageEvent.org_id == org_id,
            UsageEvent.occurred_at >= start,
            UsageEvent.occurred_at < end,
        )
    )
    in_tokens, out_tokens = q.one()
    return int(in_tokens), int(out_tokens)


async def would_exceed_token_cap(
    session: AsyncSession, org_id: int, *, est_input: int, est_output: int
) -> bool:
    plan = await get_plan_for_org(session, org_id)
    in_used, out_used = await sum_tokens_today(session, org_id)
    return (
        in_used + est_input > plan.max_input_tokens_per_day
        or out_used + est_output > plan.max_output_tokens_per_day
    )


async def enforce_task_create_limit(session: AsyncSession, org_id: int) -> None:
    """Raise QuotaExceeded if creating one more task would breach today's cap."""
    plan = await get_plan_for_org(session, org_id)
    n = await count_tasks_created_today(session, org_id)
    if n >= plan.max_tasks_per_day:
        raise QuotaExceeded(
            f"Daily task limit reached ({plan.max_tasks_per_day}). "
            f"Resets at UTC midnight."
        )
