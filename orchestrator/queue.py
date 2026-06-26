"""Task queue — single global pool with per-repo cap.

Concurrency rules:
  - At most ``settings.max_concurrent_workers`` tasks active at once.
  - At most 1 active task per repo_id (prevents working-tree conflicts).
  - Tasks with repo_id IS NULL (e.g. SIMPLE_NO_CODE research) bypass the
    per-repo cap; only the global cap applies.
  - BLOCKED_ON_AUTH is paused, not active — does not occupy a slot.

FIFO across all users. Priority (lower = first) breaks ties; default 100.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from agent.health_loop.lease import lease_held
from shared import quotas
from shared.config import settings
from shared.models import Task, TaskStatus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

# Statuses that count as "in flight" — a task here occupies its repo and its
# org's quota, so a second task must not start on the same repo. Used for
# per-repo and per-org busy detection, NOT for the global compute pool.
ACTIVE_STATUSES = {
    TaskStatus.PLANNING,
    TaskStatus.AWAITING_APPROVAL,
    TaskStatus.AWAITING_CLARIFICATION,
    TaskStatus.CODING,
    TaskStatus.PR_CREATED,
    TaskStatus.AWAITING_CI,
    TaskStatus.AWAITING_REVIEW,
    TaskStatus.ITERATING,  # ADR-017: PR open + actively re-iterating on feedback
    TaskStatus.BLOCKED,
}

# Statuses that actually consume a compute worker — an agent loop is running
# right now. The global ``max_concurrent_workers`` pool bounds THESE only.
# A task merely *waiting* (on a human approval, on CI, blocked on an error)
# holds no worker; counting it against the pool let a few parked tasks
# silently exhaust every slot and wedge all new dispatch until a human deleted
# them. The cap is admission control for QUEUED work, not a hard compute
# ceiling (trio/scaffold run states were never counted either).
RUNNING_STATUSES = {
    TaskStatus.PLANNING,
    TaskStatus.CODING,
    TaskStatus.ITERATING,
}


def is_health_loop_task(task: Task) -> bool:
    """True for the auto-heal loop's own tasks (the supervisor and its
    per-batch fix tasks), which are exempt from the lease gate — the lease
    blocks *other* work, never the loop's own.

    Matching is on ``source_id``, which is caller-influenceable: ``health:``
    and ``health-loop`` are a RESERVED namespace. The lease throttle's
    integrity therefore currently relies on no external caller using a
    ``health:``/``health-loop`` ``source_id``. Phase 5 will replace this with
    a server-owned ``TaskSource`` marker set at task creation (not settable by
    API callers) once its migration lands.
    """
    sid = task.source_id or ""
    return sid == "health-loop" or sid.startswith("health:")


async def _lease_held_safe() -> bool:
    """Lease check for the dispatch hot path — fails OPEN.

    If Redis is unreachable, treat the lease as free rather than
    letting a Redis hiccup wedge ALL task dispatch. The health loop
    throttles one subsystem; it must never be able to freeze the
    whole pipeline.
    """
    try:
        return await lease_held()
    except Exception:
        log.warning("health-loop lease check failed; treating lease as free", exc_info=True)
        return False


async def count_active(session: AsyncSession) -> int:
    """Tasks currently consuming a compute worker, across all users and repos.

    Counts ``RUNNING_STATUSES`` (an agent loop is executing) — not tasks parked
    waiting on a human or external system, which hold no worker.
    """
    result = await session.execute(
        select(func.count(Task.id)).where(Task.status.in_(RUNNING_STATUSES))
    )
    return result.scalar_one()


async def _repo_has_active_task(session: AsyncSession, repo_id: int) -> bool:
    result = await session.execute(
        select(func.count(Task.id)).where(
            Task.repo_id == repo_id,
            Task.status.in_(ACTIVE_STATUSES),
        )
    )
    return result.scalar_one() > 0


async def _org_at_concurrency_cap(session: AsyncSession, org_id: int) -> bool:
    """True when the org has hit its plan's max_concurrent_tasks.

    Defensive: if the org has no plan attached, treat as not-capped (LookupError
    from get_plan_for_org is logged but does not block dispatch — the misconfig
    will surface elsewhere as a clearer error).
    """
    try:
        plan = await quotas.get_plan_for_org(session, org_id)
    except LookupError:
        return False
    active = await quotas.count_active_tasks_for_org(session, org_id)
    return active >= plan.max_concurrent_tasks


async def can_start_task(session: AsyncSession, task: Task) -> bool:
    """Can this specific task start right now?"""
    if await count_active(session) >= settings.max_concurrent_workers:
        return False
    # Health-loop lease: while held, only the loop's own tasks may start.
    if not is_health_loop_task(task) and await _lease_held_safe():
        return False
    if task.organization_id is not None and await _org_at_concurrency_cap(
        session, task.organization_id
    ):
        return False
    return not (task.repo_id is not None and await _repo_has_active_task(session, task.repo_id))


async def next_eligible_task(session: AsyncSession) -> Task | None:
    """Return the highest-priority QUEUED task that can start right now.

    Iterates queued tasks in (priority asc, created_at asc) order and returns
    the first one that passes the global cap, per-repo cap, AND per-org cap.
    Memoizes capped orgs per-tick so we don't query the plan repeatedly when
    many tasks belong to the same capped org.
    """
    if await count_active(session) >= settings.max_concurrent_workers:
        return None

    lease_is_held = await _lease_held_safe()

    active_repos_q = await session.execute(
        select(Task.repo_id)
        .where(Task.status.in_(ACTIVE_STATUSES), Task.repo_id.is_not(None))
        .distinct()
    )
    busy_repos = {row[0] for row in active_repos_q.all()}

    capped_orgs: set[int] = set()  # memoize per-tick

    queued_q = await session.execute(
        select(Task)
        .where(Task.status == TaskStatus.QUEUED)
        .order_by(Task.priority.asc(), Task.created_at.asc())
    )
    for t in queued_q.scalars():
        if lease_is_held and not is_health_loop_task(t):
            continue
        if t.repo_id is not None and t.repo_id in busy_repos:
            continue
        if t.organization_id is not None:
            if t.organization_id in capped_orgs:
                continue
            if await _org_at_concurrency_cap(session, t.organization_id):
                capped_orgs.add(t.organization_id)
                continue
        return t
    return None
