"""Single-pool concurrency with per-repo cap of 1 and BLOCKED_ON_AUTH exemption.

Mocks ``AsyncSession.execute`` to return canned results in the order the
queue helpers call it. The order is documented in each test so a future
refactor of queue.py that changes the call sequence will fail loudly.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import queue as q
from shared.models import Task, TaskComplexity, TaskSource, TaskStatus


def _scalar_one_result(value: int) -> MagicMock:
    """Result whose .scalar_one() returns the given integer."""
    r = MagicMock()
    r.scalar_one = MagicMock(return_value=value)
    return r


def _all_result(rows: list[tuple]) -> MagicMock:
    """Result whose .all() returns the given list of (col,) tuples."""
    r = MagicMock()
    r.all = MagicMock(return_value=rows)
    return r


def _scalars_result(rows: list[Task]) -> MagicMock:
    """Result whose .scalars() iterates the given Task rows."""
    r = MagicMock()
    r.scalars = MagicMock(return_value=iter(rows))
    return r


def _make_task(
    *,
    id: int = 1,
    repo_id: int | None = None,
    status: TaskStatus = TaskStatus.QUEUED,
    complexity: TaskComplexity = TaskComplexity.SIMPLE,
    priority: int = 100,
    title: str = "t",
) -> Task:
    return Task(
        id=id,
        title=title,
        source=TaskSource.MANUAL,
        repo_id=repo_id,
        status=status,
        complexity=complexity,
        priority=priority,
    )


# ---------------------------------------------------------------------------
# can_start_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_can_start_when_under_global_cap(monkeypatch):
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 5)
    session = AsyncMock()
    # Call order: count_active → _repo_has_active_task
    session.execute = AsyncMock(
        side_effect=[_scalar_one_result(2), _scalar_one_result(0)]
    )
    candidate = _make_task(repo_id=10)
    assert await q.can_start_task(session, candidate) is True


@pytest.mark.asyncio
async def test_blocked_when_global_cap_reached(monkeypatch):
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 2)
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_scalar_one_result(2)])
    candidate = _make_task(repo_id=None)
    assert await q.can_start_task(session, candidate) is False


@pytest.mark.asyncio
async def test_blocked_when_same_repo_already_active(monkeypatch):
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 5)
    session = AsyncMock()
    # count_active returns 1 (under cap), repo has 1 active task
    session.execute = AsyncMock(
        side_effect=[_scalar_one_result(1), _scalar_one_result(1)]
    )
    candidate = _make_task(repo_id=10)
    assert await q.can_start_task(session, candidate) is False


@pytest.mark.asyncio
async def test_repoless_tasks_bypass_per_repo_cap(monkeypatch):
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 5)
    session = AsyncMock()
    # repo_id is None → second query is skipped
    session.execute = AsyncMock(side_effect=[_scalar_one_result(1)])
    candidate = _make_task(repo_id=None)
    assert await q.can_start_task(session, candidate) is True


# ---------------------------------------------------------------------------
# next_eligible_task — head-of-line skip + BLOCKED_ON_AUTH exemption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_eligible_skips_repo_blocked_tasks(monkeypatch):
    """A task on a busy repo must not head-of-line-block tasks on other repos."""
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 5)
    session = AsyncMock()
    # Call order: count_active → distinct active repo_ids → queued tasks
    queued = [
        _make_task(id=2, title="head-of-line", repo_id=1, priority=100),
        _make_task(id=3, title="should-run", repo_id=2, priority=100),
    ]
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_result(1),  # one active task globally
            _all_result([(1,)]),  # repo 1 is busy
            _scalars_result(queued),
        ]
    )
    next_task = await q.next_eligible_task(session)
    assert next_task is not None
    assert next_task.title == "should-run"


@pytest.mark.asyncio
async def test_next_eligible_returns_none_when_global_cap_reached(monkeypatch):
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 2)
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[_scalar_one_result(2)])
    assert await q.next_eligible_task(session) is None


@pytest.mark.asyncio
async def test_next_eligible_picks_repoless_task_even_when_repos_busy(monkeypatch):
    monkeypatch.setattr(q.settings, "max_concurrent_workers", 5)
    session = AsyncMock()
    queued = [
        _make_task(id=2, title="head-of-line", repo_id=1),
        _make_task(id=3, title="repoless", repo_id=None),
    ]
    session.execute = AsyncMock(
        side_effect=[
            _scalar_one_result(1),
            _all_result([(1,)]),  # repo 1 busy
            _scalars_result(queued),
        ]
    )
    next_task = await q.next_eligible_task(session)
    assert next_task.title == "repoless"


# ---------------------------------------------------------------------------
# ACTIVE_STATUSES does NOT include BLOCKED_ON_AUTH (paused, not active)
# ---------------------------------------------------------------------------


def test_blocked_on_auth_is_not_an_active_status():
    """The BLOCKED_ON_AUTH status must not occupy a slot."""
    assert TaskStatus.BLOCKED_ON_AUTH not in q.ACTIVE_STATUSES
