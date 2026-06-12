"""Regression: a BLOCKED task must not strand new/retried tasks on its repo.

Incident (task #324 → #327, 2026-06-12): #324 failed the intent check and
parked in BLOCKED on repo 26. Its re-trigger #327 was QUEUED on the same repo.
Because BLOCKED counted as an "active" per-repo slot, the dispatcher saw repo 26
as permanently busy and skipped #327 forever — the blocked task deadlocked its
own retry. BLOCKED is parked-awaiting-human (like BLOCKED_ON_AUTH); it must not
occupy a worker, repo, or org slot.
"""

from __future__ import annotations

import uuid

import pytest

from orchestrator import queue as q
from shared import quotas
from shared.models import Organization, Plan, Repo, Task, TaskStatus


async def _seed_org_and_repo(session) -> tuple[Organization, Repo]:
    plan = Plan(
        name=f"plan-{uuid.uuid4().hex[:6]}",
        max_concurrent_tasks=10,
        max_tasks_per_day=100,
        max_input_tokens_per_day=10_000_000,
        max_output_tokens_per_day=2_500_000,
        max_members=5,
    )
    session.add(plan)
    await session.flush()
    org = Organization(name="o", slug=f"o-{uuid.uuid4().hex[:6]}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(name=f"r-{uuid.uuid4().hex[:6]}", url="https://x/r", organization_id=org.id)
    session.add(repo)
    await session.flush()
    return org, repo


def test_blocked_is_not_an_active_status():
    """BLOCKED is parked awaiting human — it must not occupy a slot."""
    assert TaskStatus.BLOCKED not in q.ACTIVE_STATUSES
    assert TaskStatus.BLOCKED not in quotas._ACTIVE_STATUSES


@pytest.mark.asyncio
async def test_blocked_task_does_not_strand_retry_on_same_repo(session) -> None:
    org, repo = await _seed_org_and_repo(session)

    session.add(Task(title="failed-attempt", description="", source="manual",
                     source_id=f"b-{uuid.uuid4().hex[:6]}", status=TaskStatus.BLOCKED,
                     repo_id=repo.id, organization_id=org.id, priority=100))
    retry = Task(title="re-trigger", description="", source="manual",
                 source_id=f"q-{uuid.uuid4().hex[:6]}", status=TaskStatus.QUEUED,
                 repo_id=repo.id, organization_id=org.id, priority=100)
    session.add(retry)
    await session.flush()

    picked = await q.next_eligible_task(session)
    assert picked is not None, "blocked task on the repo deadlocked its own retry"
    assert picked.title == "re-trigger"
    assert await q.can_start_task(session, retry) is True
