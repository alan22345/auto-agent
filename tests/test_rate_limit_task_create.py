"""POST /api/tasks 429s when org is over plan.max_tasks_per_day."""

from __future__ import annotations

import os
import uuid

import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from shared.config import settings
from shared.models import Organization, Plan, Task, User

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Per-test engine/session bound to the test's own event loop
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    """Fresh engine + session factory for the current event loop.

    Creates a real DB session, seeds inside a transaction that is rolled
    back after the test finishes so no artifacts survive.

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_org_with_user(
    session: AsyncSession, *, max_tasks_per_day: int
) -> tuple[Organization, User]:
    plan = Plan(
        name=f"tight-{uuid.uuid4().hex[:6]}",
        max_concurrent_tasks=10,
        max_tasks_per_day=max_tasks_per_day,
        max_input_tokens_per_day=10_000_000,
        max_output_tokens_per_day=10_000_000,
        max_members=10,
    )
    session.add(plan)
    await session.flush()
    org = Organization(name="t", slug=f"t-{uuid.uuid4().hex[:6]}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    user = User(
        username=f"u-{uuid.uuid4().hex[:6]}",
        display_name=f"Test User {uuid.uuid4().hex[:4]}",
        email=f"{uuid.uuid4().hex[:8]}@x.test",
        password_hash="x",
        claude_auth_status="paired",
        organization_id=org.id,
    )
    session.add(user)
    await session.flush()
    return org, user


def _mint_session_cookie(*, user_id: int, current_org_id: int) -> str:
    """Mint an auto_agent_session JWT the same way the app would."""
    payload = {"user_id": user_id, "current_org_id": current_org_id}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_create_returns_429_when_over_daily_cap(db) -> None:
    seed_session, factory = db
    org, user = await _seed_org_with_user(seed_session, max_tasks_per_day=2)
    # Flush so the data is visible within this transaction.
    await seed_session.flush()

    from orchestrator.router import router
    from shared.database import get_session

    app = FastAPI()
    app.include_router(router, prefix="/api")

    # Override get_session with a fresh session from our test-local factory
    # (same event loop, same transaction, so seeded data is visible).
    async def _override_session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = _override_session
    cookie = _mint_session_cookie(user_id=user.id, current_org_id=org.id)

    # Commit the seed data so the endpoint's own session can read it.
    await seed_session.commit()

    task_ids: list[int] = []
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        cookies={"auto_agent_session": cookie},
    ) as c:
        # First two should pass (unique titles to avoid dedup short-circuit).
        for i in range(2):
            r = await c.post(
                "/api/tasks",
                json={
                    "title": f"task-{uuid.uuid4().hex[:8]}",
                    "description": "",
                    "source": "manual",
                    "source_id": f"i{i}-{uuid.uuid4().hex[:4]}",
                },
            )
            assert r.status_code == 200, r.text
            task_ids.append(r.json()["id"])
        # Third should 429.
        r = await c.post(
            "/api/tasks",
            json={
                "title": f"task-{uuid.uuid4().hex[:8]}",
                "description": "",
                "source": "manual",
                "source_id": f"i2-{uuid.uuid4().hex[:4]}",
            },
        )
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        assert "daily task limit" in r.json()["detail"].lower()

    # Cleanup committed rows (endpoint committed its own session).
    # Use a fresh session from our loop-local factory.
    async with factory() as cleanup:
        await cleanup.execute(sa_delete(Task).where(Task.organization_id == org.id))
        await cleanup.execute(sa_delete(User).where(User.id == user.id))
        await cleanup.execute(sa_delete(Organization).where(Organization.id == org.id))
        await cleanup.execute(sa_delete(Plan).where(Plan.id == org.plan_id))
        await cleanup.commit()
