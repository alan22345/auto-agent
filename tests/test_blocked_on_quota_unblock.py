"""When today's tokens are under the cap, BLOCKED_ON_QUOTA → QUEUED."""

from __future__ import annotations

import uuid

import pytest

from orchestrator.unblock import unblock_quota_paused
from shared.models import TaskStatus
from tests.helpers import make_org_and_task

pytestmark = pytest.mark.asyncio


async def test_under_cap_unblocks(session) -> None:
    slug = f"goq-unblk-{uuid.uuid4().hex[:8]}"
    _org, task = await make_org_and_task(session, status=TaskStatus.BLOCKED_ON_QUOTA, slug=slug)
    # No usage_events for today — under cap.
    await session.flush()

    moved = await unblock_quota_paused(session)
    await session.refresh(task)
    assert moved == 1
    assert task.status == TaskStatus.QUEUED


async def test_no_blocked_on_quota_tasks_returns_zero(session) -> None:
    # No tasks at all in the transaction scope — sweep returns 0.
    moved = await unblock_quota_paused(session)
    assert moved == 0
