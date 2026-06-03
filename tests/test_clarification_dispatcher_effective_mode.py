"""ARCHITECT_CLARIFICATION_NEEDED routes by *effective* mode, not the legacy
``Task.freeform_mode`` boolean.

Regression for task 41 (2026-06-03): the task was made freeform via
``Task.mode_override='freeform'`` with the ``freeform_mode`` column left False.
``on_architect_clarification_needed`` gated on ``task.freeform_mode`` directly,
so it took the human-in-loop branch and escaped to the user instead of letting
the PO standin auto-answer. The dispatcher now resolves the effective mode
(``resolve_effective_mode``), which honors ``mode_override``.

These are DB-free: the session/get_task/repo layer is mocked so the routing
decision can be asserted without a migrated Postgres.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest

from shared.events import Event, TaskEventType

pytestmark = pytest.mark.asyncio


class _FakeSession:
    """Async-context-manager stub whose ``get`` returns a fixed repo."""

    def __init__(self, repo) -> None:
        self.get = AsyncMock(return_value=repo)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


def _event(task_id: int) -> Event:
    return Event(
        type=TaskEventType.ARCHITECT_CLARIFICATION_NEEDED,
        task_id=task_id,
        payload={"question": "Q?"},
    )


async def _dispatch(task, repo, po_mock):
    import run

    def _fake_spawn(coro, *, label):
        coro.close()  # consume so there's no 'never awaited' warning

    with (
        patch("run.async_session", lambda: _FakeSession(repo)),
        patch("run.get_task", AsyncMock(return_value=task)),
        patch("agent.po_agent.answer_architect_question", po_mock),
        patch("run._spawn_bg", _fake_spawn),
    ):
        await run.on_architect_clarification_needed(_event(task.id))


async def test_mode_override_freeform_dispatches_po_standin(publisher):
    """freeform_mode=False but mode_override='freeform' → PO standin answers."""
    from shared.models import TaskStatus, TrioPhase

    task = types.SimpleNamespace(
        id=41,
        status=TaskStatus.AWAITING_CLARIFICATION,
        trio_phase=TrioPhase.ARCHITECTING,
        mode_override="freeform",
        freeform_mode=False,
        repo_id=7,
    )
    repo = types.SimpleNamespace(id=7, mode="human_in_loop")
    po_mock = AsyncMock()

    await _dispatch(task, repo, po_mock)

    po_mock.assert_called_once_with(41)
    assert not [e for e in publisher.events if e.type == TaskEventType.CLARIFICATION_NEEDED]


async def test_mode_override_human_on_freeform_repo_escalates(publisher):
    """mode_override='human_in_loop' wins over a freeform repo → ask the user."""
    from shared.models import TaskStatus, TrioPhase

    task = types.SimpleNamespace(
        id=42,
        status=TaskStatus.AWAITING_CLARIFICATION,
        trio_phase=TrioPhase.ARCHITECTING,
        mode_override="human_in_loop",
        freeform_mode=True,
        repo_id=7,
    )
    repo = types.SimpleNamespace(id=7, mode="freeform")
    po_mock = AsyncMock()

    await _dispatch(task, repo, po_mock)

    po_mock.assert_not_called()
    needed = [e for e in publisher.events if e.type == TaskEventType.CLARIFICATION_NEEDED]
    assert len(needed) == 1
    assert needed[0].payload["phase"] == "trio_architect"
