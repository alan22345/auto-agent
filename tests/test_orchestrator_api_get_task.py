"""``_orchestrator_api.get_task`` must read the DB in-process, not the HTTP loopback.

Regression for the 2026-06-03 clarification-routing failure: the agent-side
``get_task`` went through ``GET /tasks/{id}``, which requires org-scoped auth
(``current_org_id_dep``). The agent has no session/token, so the call 401'd and
``get_task`` returned ``None``. ``route_human_message`` then silently dropped
every inbound message at ``if not task: return`` — the user's Slack clarification
reply never reached ``handle_clarification_inbound``.

The fix reads in-process via ``shared.database`` (the same pattern
``transition_task`` already uses), so no auth/loopback is involved.
"""

from __future__ import annotations

import pytest

from shared.types import TaskData

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Session:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **k):
        return _Result(self._row)


async def test_get_task_reads_db_and_never_uses_http(monkeypatch):
    import agent.lifecycle._orchestrator_api as api
    import orchestrator.router as router

    sentinel_row = object()
    expected = TaskData(
        id=41, title="t", description="d", source="manual",
        status="awaiting_clarification",
    )

    monkeypatch.setattr(api, "async_session", lambda: _Session(sentinel_row))
    monkeypatch.setattr(router, "_task_to_response", lambda t: expected)

    def _no_http(*a, **k):
        raise AssertionError("get_task must not use the HTTP loopback")

    monkeypatch.setattr(api.httpx, "AsyncClient", _no_http)

    got = await api.get_task(41)
    assert got is expected


async def test_get_task_returns_none_when_missing(monkeypatch):
    import agent.lifecycle._orchestrator_api as api

    monkeypatch.setattr(api, "async_session", lambda: _Session(None))

    def _no_http(*a, **k):
        raise AssertionError("get_task must not use the HTTP loopback")

    monkeypatch.setattr(api.httpx, "AsyncClient", _no_http)

    assert await api.get_task(999) is None
