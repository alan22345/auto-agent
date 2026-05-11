"""Pure read helpers over usage_events / tasks / organizations.plans."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from shared import quotas

pytestmark = pytest.mark.asyncio


async def _seed_org_with_plan(session, name: str):
    from shared.models import Organization, Plan
    plan = Plan(
        name=f"plan-{name}",
        max_concurrent_tasks=1,
        max_tasks_per_day=5,
        max_input_tokens_per_day=1_000_000,
        max_output_tokens_per_day=250_000,
        max_members=3,
        monthly_price_cents=0,
    )
    session.add(plan)
    await session.flush()
    org = Organization(name=f"Org {name}", slug=f"org-{name}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    return org


async def test_get_plan_for_org_returns_attached_plan(session) -> None:
    org = await _seed_org_with_plan(session, "a")
    plan = await quotas.get_plan_for_org(session, org.id)
    assert plan.name == "plan-a"


async def test_count_active_tasks_for_org_excludes_other_orgs(session) -> None:
    from shared.models import Task, TaskStatus
    org_a = await _seed_org_with_plan(session, "ax")
    org_b = await _seed_org_with_plan(session, "bx")
    session.add(Task(title="t1", description="", source="manual", source_id="x1",
                     status=TaskStatus.CODING, organization_id=org_a.id))
    session.add(Task(title="t2", description="", source="manual", source_id="x2",
                     status=TaskStatus.CODING, organization_id=org_b.id))
    await session.flush()
    assert await quotas.count_active_tasks_for_org(session, org_a.id) == 1
    assert await quotas.count_active_tasks_for_org(session, org_b.id) == 1


async def test_count_tasks_created_today_excludes_yesterday(session) -> None:
    from shared.models import Task, TaskStatus
    org = await _seed_org_with_plan(session, "tx")
    today = dt.datetime.now(dt.UTC)
    yesterday = today - dt.timedelta(days=1)
    session.add(Task(title="today", description="", source="manual", source_id="td1",
                     status=TaskStatus.QUEUED, organization_id=org.id,
                     created_at=today))
    session.add(Task(title="yest", description="", source="manual", source_id="td2",
                     status=TaskStatus.QUEUED, organization_id=org.id,
                     created_at=yesterday))
    await session.flush()
    n = await quotas.count_tasks_created_today(session, org.id)
    assert n == 1


async def test_sum_tokens_today_excludes_yesterday(session) -> None:
    from shared.models import UsageEvent
    org = await _seed_org_with_plan(session, "tok")
    today = dt.datetime.now(dt.UTC)
    yesterday = today - dt.timedelta(days=1)
    session.add(UsageEvent(
        org_id=org.id, kind="llm_call", model="x",
        input_tokens=100, output_tokens=50, cost_cents=Decimal(0),
        occurred_at=today,
    ))
    session.add(UsageEvent(
        org_id=org.id, kind="llm_call", model="x",
        input_tokens=999, output_tokens=999, cost_cents=Decimal(0),
        occurred_at=yesterday,
    ))
    await session.flush()
    in_today, out_today = await quotas.sum_tokens_today(session, org.id)
    assert in_today == 100
    assert out_today == 50


async def test_would_exceed_token_cap(session) -> None:
    from shared.models import UsageEvent
    org = await _seed_org_with_plan(session, "cap")
    today = dt.datetime.now(dt.UTC)
    session.add(UsageEvent(
        org_id=org.id, kind="llm_call", model="x",
        input_tokens=900_000, output_tokens=0, cost_cents=Decimal(0),
        occurred_at=today,
    ))
    await session.flush()
    assert await quotas.would_exceed_token_cap(session, org.id, est_input=200_000, est_output=0)
    assert not await quotas.would_exceed_token_cap(session, org.id, est_input=50_000, est_output=0)


def test_quota_exceeded_is_exception() -> None:
    assert issubclass(quotas.QuotaExceeded, Exception)
