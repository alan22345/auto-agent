"""Test helpers for Phase 4 quota tests.

Keep this file tiny — only seed-and-return helpers that are reused across
≥ 2 test files. One-off seeding lives in the test itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_UNSET = object()


async def _ensure_default_plan(session: AsyncSession) -> Plan:
    from shared.models import Plan

    q = await session.execute(select(Plan).where(Plan.name == "free"))
    plan = q.scalar_one_or_none()
    if plan is not None:
        return plan
    plan = Plan(
        name="free",
        max_concurrent_tasks=1,
        max_tasks_per_day=5,
        max_input_tokens_per_day=1_000_000,
        max_output_tokens_per_day=250_000,
        max_members=3,
        monthly_price_cents=0,
    )
    session.add(plan)
    await session.flush()
    return plan


async def make_org_and_task(
    session: AsyncSession,
    *,
    status: TaskStatus = _UNSET,
    slug: str = "test-org",
) -> tuple[Organization, Task]:
    from shared.models import Organization, Task, TaskStatus

    if status is _UNSET:
        status = TaskStatus.QUEUED
    plan = await _ensure_default_plan(session)
    org = Organization(name="Test Org", slug=slug, plan_id=plan.id)
    session.add(org)
    await session.flush()
    task = Task(
        title="t",
        description="",
        source="manual",
        source_id=f"src-{org.id}",
        status=status,
        organization_id=org.id,
    )
    session.add(task)
    await session.flush()
    return org, task
