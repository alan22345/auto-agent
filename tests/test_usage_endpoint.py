"""GET /api/usage/summary — returns plan caps + today's totals scoped to current org."""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    """Fresh engine + session factory for the current event loop.

    Skips if DATABASE_URL is not set.
    """
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set — Phase 4 DB tests need real Postgres")

    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as s:
        await s.begin()
        try:
            yield s, factory
        finally:
            await s.rollback()
            await s.close()
    await engine.dispose()


async def _ensure_free_plan(session: AsyncSession):
    """Return the existing free plan or insert a new one."""
    from shared.models import Plan

    result = await session.execute(select(Plan).where(Plan.name == "free"))
    plan = result.scalar_one_or_none()
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


async def test_usage_summary_returns_plan_and_current_totals(db) -> None:
    seed_session, factory = db

    from shared.models import Organization, Task

    plan = await _ensure_free_plan(seed_session)

    slug = f"u-{uuid.uuid4().hex[:6]}"
    org = Organization(name="Test Org", slug=slug, plan_id=plan.id)
    seed_session.add(org)
    await seed_session.flush()

    task = Task(
        title="seed-task",
        description="",
        source="manual",
        source_id=f"src-{org.id}",
        status="queued",
        organization_id=org.id,
    )
    seed_session.add(task)
    await seed_session.flush()

    # Commit so the endpoint's own session can read the seeded data.
    await seed_session.commit()

    from orchestrator.auth import current_org_id as current_org_id_dep
    from orchestrator.router import router
    from shared.database import get_session

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _fake_org() -> int:
        return org.id

    async def _override_session():
        async with factory() as s:
            yield s

    app.dependency_overrides[current_org_id_dep] = _fake_org
    app.dependency_overrides[get_session] = _override_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/usage/summary")

    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["plan"]["name"] == "free"
    assert payload["plan"]["max_concurrent_tasks"] == 1
    assert payload["plan"]["max_tasks_per_day"] == 5
    assert payload["active_tasks"] >= 0
    assert payload["tasks_today"] >= 1  # the seeded task counts
    assert payload["input_tokens_today"] == 0
    assert payload["output_tokens_today"] == 0

    # Cleanup committed rows using a fresh session.
    # Do NOT delete the shared free plan — it may be used by other tests.
    async with factory() as cleanup:
        await cleanup.execute(sa_delete(Task).where(Task.organization_id == org.id))
        await cleanup.execute(sa_delete(Organization).where(Organization.id == org.id))
        await cleanup.commit()
