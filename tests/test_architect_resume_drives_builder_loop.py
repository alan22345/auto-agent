"""After a clarification answer, the resumed architect must drive the per-item
builder loop — not strand the parent at backlog-emit.

Regression for task 41 (2026-06-03): ``on_architect_clarification_resolved``
spawned ``architect.resume``, which only (re)emits the backlog and returns.
Nothing then re-entered ``run_trio_parent`` (the owner of the per-item loop),
so the parent sat in TRIO_EXECUTING / trio_phase=ARCHITECTING with a populated
backlog but zero builders dispatched. The fix re-enters ``run_trio_parent``
once resume has produced a backlog (status stays TRIO_EXECUTING); it is skipped
when resume re-asked (→ AWAITING_CLARIFICATION) or blocked.

DB-free: the session/get_task/architect.resume/run_trio_parent layer is mocked.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.asyncio


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _run(monkeypatch, task):
    import run

    resume_mock = AsyncMock()
    parent_mock = AsyncMock()
    monkeypatch.setattr("agent.lifecycle.trio.architect.resume", resume_mock)
    monkeypatch.setattr("agent.lifecycle.trio.run_trio_parent", parent_mock)
    monkeypatch.setattr(run, "async_session", lambda: _FakeSession())
    monkeypatch.setattr(run, "get_task", AsyncMock(return_value=task))

    await run._resume_architect_and_drive(task.id if task else 41)
    return resume_mock, parent_mock


async def test_drives_builder_loop_when_backlog_emitted(monkeypatch):
    from shared.models import TaskStatus

    task = types.SimpleNamespace(
        id=41,
        status=TaskStatus.TRIO_EXECUTING,
        trio_backlog=[{"id": "a", "status": "pending"}],
    )
    resume_mock, parent_mock = await _run(monkeypatch, task)

    resume_mock.assert_awaited_once_with(41)
    parent_mock.assert_awaited_once_with(task)


async def test_skips_builder_loop_when_reasked(monkeypatch):
    """Resume re-asked → status back to AWAITING_CLARIFICATION → do not drive."""
    from shared.models import TaskStatus

    task = types.SimpleNamespace(
        id=41,
        status=TaskStatus.AWAITING_CLARIFICATION,
        trio_backlog=[{"id": "a", "status": "pending"}],
    )
    resume_mock, parent_mock = await _run(monkeypatch, task)

    resume_mock.assert_awaited_once_with(41)
    parent_mock.assert_not_awaited()


async def test_skips_builder_loop_when_no_backlog(monkeypatch):
    from shared.models import TaskStatus

    task = types.SimpleNamespace(
        id=41, status=TaskStatus.TRIO_EXECUTING, trio_backlog=None,
    )
    resume_mock, parent_mock = await _run(monkeypatch, task)

    resume_mock.assert_awaited_once_with(41)
    parent_mock.assert_not_awaited()
