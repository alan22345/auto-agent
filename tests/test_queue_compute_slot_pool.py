"""The global worker pool must bound *compute*, not human-waiting work.

Regression for the "breaks after a while" wedge: tasks parked in human- or
external-waiting states (AWAITING_APPROVAL, BLOCKED, AWAITING_CI, …) were
counted against ``max_concurrent_workers``. With only a handful of slots, a
couple of parked tasks silently exhausted the pool and *no* new task could
start until a human deleted the parked ones. A task waiting on a human is not
using a worker — only actively-computing states should occupy a compute slot.
"""

from __future__ import annotations

import uuid

import pytest

from orchestrator import queue as q
from shared.config import settings
from shared.models import Organization, Plan, Task, TaskStatus

pytestmark = pytest.mark.asyncio


async def _seed_org(session) -> Organization:
    """An org whose plan cap is high enough that only the *global* compute
    pool — never the per-org cap — can gate dispatch in these tests."""
    plan = Plan(
        name=f"plan-{uuid.uuid4().hex[:6]}",
        max_concurrent_tasks=10_000,
        max_tasks_per_day=10_000,
        max_input_tokens_per_day=10_000_000,
        max_output_tokens_per_day=2_500_000,
        max_members=5,
    )
    session.add(plan)
    await session.flush()
    org = Organization(name="o", slug=f"o-{uuid.uuid4().hex[:6]}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    return org


def _task(status: TaskStatus, *, org_id: int, title: str, priority: int = 1) -> Task:
    return Task(
        title=title,
        description="",
        source="manual",
        source_id=f"{title}-{uuid.uuid4().hex[:6]}",
        status=status,
        priority=priority,
        organization_id=org_id,
    )


async def test_human_waiting_tasks_do_not_consume_compute_slots(session) -> None:
    org = await _seed_org(session)
    # Fill every compute slot with tasks that are merely *waiting* — not running.
    waiting = [TaskStatus.AWAITING_APPROVAL, TaskStatus.BLOCKED]
    for i in range(settings.max_concurrent_workers):
        session.add(_task(waiting[i % len(waiting)], org_id=org.id, title=f"parked-{i}"))

    target = _task(TaskStatus.QUEUED, org_id=org.id, title="queued", priority=1)
    session.add(target)
    await session.flush()

    # The parked tasks hold no compute, so a queued task must still start.
    assert await q.can_start_task(session, target) is True
    picked = await q.next_eligible_task(session)
    assert picked is not None and picked.title == "queued"


async def test_running_tasks_do_consume_compute_slots(session) -> None:
    org = await _seed_org(session)
    # Actively-computing tasks DO occupy the pool — the cap must still hold.
    for i in range(settings.max_concurrent_workers):
        session.add(_task(TaskStatus.CODING, org_id=org.id, title=f"running-{i}"))

    target = _task(TaskStatus.QUEUED, org_id=org.id, title="queued", priority=1)
    session.add(target)
    await session.flush()

    assert await q.can_start_task(session, target) is False
    assert await q.next_eligible_task(session) is None
