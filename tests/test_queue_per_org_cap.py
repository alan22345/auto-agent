"""Per-org concurrency cap blocks org A but lets org B through."""

from __future__ import annotations

import uuid

import pytest

from orchestrator import queue as q
from shared.models import Organization, Plan, Task, TaskStatus

pytestmark = pytest.mark.asyncio


async def _seed_plan(session, cap: int) -> Plan:
    plan = Plan(
        name=f"plan-cap-{uuid.uuid4().hex[:6]}",
        max_concurrent_tasks=cap,
        max_tasks_per_day=100,
        max_input_tokens_per_day=10_000_000,
        max_output_tokens_per_day=2_500_000,
        max_members=5,
    )
    session.add(plan)
    await session.flush()
    return plan


async def _seed_org(session, plan: Plan, slug: str) -> Organization:
    org = Organization(name=slug, slug=f"{slug}-{uuid.uuid4().hex[:6]}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    return org


async def test_org_at_cap_skipped_other_org_dispatched(session) -> None:
    plan = await _seed_plan(session, cap=1)
    org_a = await _seed_org(session, plan, "a")
    org_b = await _seed_org(session, plan, "b")

    session.add(Task(title="a-active", description="", source="manual", source_id=f"s1-{uuid.uuid4().hex[:6]}",
                     status=TaskStatus.CODING, organization_id=org_a.id))
    session.add(Task(title="a-queued", description="", source="manual", source_id=f"s2-{uuid.uuid4().hex[:6]}",
                     status=TaskStatus.QUEUED, organization_id=org_a.id, priority=1))
    session.add(Task(title="b-queued", description="", source="manual", source_id=f"s3-{uuid.uuid4().hex[:6]}",
                     status=TaskStatus.QUEUED, organization_id=org_b.id, priority=2))
    await session.flush()

    picked = await q.next_eligible_task(session)
    assert picked is not None, "Expected to pick org B's queued task"
    assert picked.title == "b-queued"


async def test_can_start_task_blocks_when_org_at_cap(session) -> None:
    plan = await _seed_plan(session, cap=1)
    org = await _seed_org(session, plan, "x")

    session.add(Task(title="active", description="", source="manual", source_id=f"c1-{uuid.uuid4().hex[:6]}",
                     status=TaskStatus.CODING, organization_id=org.id))
    target = Task(title="queued", description="", source="manual", source_id=f"c2-{uuid.uuid4().hex[:6]}",
                  status=TaskStatus.QUEUED, organization_id=org.id, priority=1)
    session.add(target)
    await session.flush()

    assert await q.can_start_task(session, target) is False
