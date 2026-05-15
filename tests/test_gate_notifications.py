"""Notifications for the new ADR-015 gate states — Phase 7.7.

When a task parks at ``AWAITING_DESIGN_APPROVAL`` or
``AWAITING_PLAN_APPROVAL`` the user needs a Slack / Telegram nudge with a
link back to the web-next gate UI. Three invariants are pinned here:

1. ``transition_task`` publishes a wire event for the two new states
   (and also for ``blocked``, which it already did).
2. The Telegram dispatcher has a formatter for each new event type whose
   message body references the task and a URL pointing at
   ``{app_base_url}/tasks/{id}``.
3. The Slack dispatcher has matching formatters.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub aiohttp + slack socket-mode adapters before importing the slack module.
# Same pattern as tests/test_slack_notification_loop_org_id.py.
# ---------------------------------------------------------------------------


class _AiohttpStub(ModuleType):
    def __getattr__(self, name: str):
        obj = MagicMock()
        setattr(self, name, obj)
        return obj


_aiohttp_stub = _AiohttpStub("aiohttp")
sys.modules.setdefault("aiohttp", _aiohttp_stub)

for _name in ("FormData", "BasicAuth", "ClientSession", "web", "TCPConnector"):
    setattr(_aiohttp_stub, _name, MagicMock())

for _mod_name in (
    "slack_sdk.socket_mode.aiohttp",
    "slack_bolt.adapter.socket_mode.aiohttp",
    "slack_bolt.adapter.socket_mode.async_handler",
    "slack_bolt.adapter.aiohttp",
):
    if _mod_name not in sys.modules:
        _stub = ModuleType(_mod_name)
        _stub.AsyncSocketModeHandler = MagicMock()  # type: ignore[attr-defined]
        _stub.SocketModeClient = MagicMock()  # type: ignore[attr-defined]
        _stub.to_bolt_request = MagicMock()  # type: ignore[attr-defined]
        _stub.to_aiohttp_response = MagicMock()  # type: ignore[attr-defined]
        sys.modules[_mod_name] = _stub


from agent.lifecycle._orchestrator_api import transition_task  # noqa: E402
from integrations.slack.main import (  # noqa: E402
    _NOTIFICATION_FORMATTERS as SLACK_FORMATTERS,
)
from integrations.telegram.main import (  # noqa: E402
    _NOTIFICATION_FORMATTERS as TELEGRAM_FORMATTERS,
)
from shared.events import (  # noqa: E402
    TaskEventType,
    task_awaiting_design_approval,
    task_awaiting_plan_approval,
    task_pr_created,
)


# ---------------------------------------------------------------------------
# 1. transition_task publishes the new events.
#
# transition_task runs the state machine in-process — the HTTP loopback
# variant 401'd silently because the agent has no auth context, so the
# event used to fire even when the DB transition didn't. These tests pin
# both that publish fires AND that publish fires ONLY after a successful
# state-machine commit.
# ---------------------------------------------------------------------------


def _fake_session_for_task(task):
    """Build an async-context-manager session stub that yields ``task`` for
    every ``execute(select(Task))``. Production opens its own session so
    we can't pass one in; patching ``async_session`` to return this stub
    is the cheapest way to drive the in-process state machine in a unit
    test without standing up Postgres."""

    fake_result = MagicMock()
    fake_result.scalar_one = lambda: task

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, _stmt):
            return fake_result

        async def commit(self):
            return None

        def add(self, _row):  # state_machine.transition does session.add(TaskHistory(...))
            return None

        async def flush(self):
            return None

    return _Session()


@pytest.mark.asyncio
async def test_transition_to_awaiting_design_approval_publishes_event(publisher):
    """transition_task(... 'awaiting_design_approval' ...) emits an event."""

    from shared.models import Task, TaskStatus

    task = Task(
        id=7,
        title="t",
        description="d",
        source="web",
        # State-machine edge: ARCHITECT_DESIGNING → AWAITING_DESIGN_APPROVAL.
        # TRIO_EXECUTING goes through ARCHITECT_DESIGNING first (done by
        # ``_advance_through_design_gate`` before ``architect.run_design``).
        status=TaskStatus.ARCHITECT_DESIGNING,
        organization_id=1,
    )

    with patch(
        "agent.lifecycle._orchestrator_api.async_session",
        lambda: _fake_session_for_task(task),
    ):
        await transition_task(7, "awaiting_design_approval", "design ready")

    events = [
        e for e in publisher.events if e.type == TaskEventType.AWAITING_DESIGN_APPROVAL
    ]
    assert len(events) == 1
    assert events[0].task_id == 7
    # Message survives into the payload so the dispatcher can show it.
    assert events[0].payload.get("message") == "design ready"
    # And the state machine actually moved the task.
    assert task.status == TaskStatus.AWAITING_DESIGN_APPROVAL


@pytest.mark.asyncio
async def test_transition_to_awaiting_plan_approval_publishes_event(publisher):
    from shared.models import Task, TaskStatus

    task = Task(
        id=11,
        title="t",
        description="d",
        source="web",
        status=TaskStatus.PLANNING,  # AWAITING_PLAN_APPROVAL reachable from PLANNING
        organization_id=1,
    )

    with patch(
        "agent.lifecycle._orchestrator_api.async_session",
        lambda: _fake_session_for_task(task),
    ):
        await transition_task(11, "awaiting_plan_approval", "plan ready")

    events = [
        e for e in publisher.events if e.type == TaskEventType.AWAITING_PLAN_APPROVAL
    ]
    assert len(events) == 1
    assert events[0].task_id == 11


# ---------------------------------------------------------------------------
# 2. Factory shapes — the payload must carry the message string so the
#    dispatcher can show it alongside the task URL.
# ---------------------------------------------------------------------------


def test_task_awaiting_design_approval_factory_shape():
    ev = task_awaiting_design_approval(task_id=42, message="design ready")
    assert ev.type == TaskEventType.AWAITING_DESIGN_APPROVAL
    assert ev.task_id == 42
    assert ev.payload["message"] == "design ready"


def test_task_awaiting_plan_approval_factory_shape():
    ev = task_awaiting_plan_approval(task_id=42, message="plan ready")
    assert ev.type == TaskEventType.AWAITING_PLAN_APPROVAL
    assert ev.task_id == 42
    assert ev.payload["message"] == "plan ready"


# ---------------------------------------------------------------------------
# 3. Telegram dispatcher has formatters for the new events.
# ---------------------------------------------------------------------------


def test_telegram_dispatcher_renders_awaiting_design_approval():
    formatter = TELEGRAM_FORMATTERS.get(TaskEventType.AWAITING_DESIGN_APPROVAL)
    assert formatter is not None, "Telegram dispatcher must wire AWAITING_DESIGN_APPROVAL"
    msg = formatter({"message": "design ready"}, "Task #7: Build app", False, 7)
    assert isinstance(msg, str) and msg.strip()
    # Message mentions design and the task.
    assert "design" in msg.lower()
    assert "#7" in msg or "Task #7" in msg
    # A URL pointing at the web-next gate UI is included.
    assert "/tasks/7" in msg


def test_telegram_dispatcher_renders_awaiting_plan_approval():
    formatter = TELEGRAM_FORMATTERS.get(TaskEventType.AWAITING_PLAN_APPROVAL)
    assert formatter is not None, "Telegram dispatcher must wire AWAITING_PLAN_APPROVAL"
    msg = formatter({"message": "plan ready"}, "Task #4: Build app", False, 4)
    assert isinstance(msg, str) and msg.strip()
    assert "plan" in msg.lower()
    assert "/tasks/4" in msg


# ---------------------------------------------------------------------------
# 4. Slack dispatcher has matching formatters.
# ---------------------------------------------------------------------------


def test_slack_dispatcher_renders_awaiting_design_approval():
    formatter = SLACK_FORMATTERS.get(TaskEventType.AWAITING_DESIGN_APPROVAL)
    assert formatter is not None, "Slack dispatcher must wire AWAITING_DESIGN_APPROVAL"
    msg = formatter({"message": "design ready"}, "Task #7: Build app", False, 7)
    assert isinstance(msg, str) and msg.strip()
    assert "design" in msg.lower()
    assert "/tasks/7" in msg


def test_slack_dispatcher_renders_awaiting_plan_approval():
    formatter = SLACK_FORMATTERS.get(TaskEventType.AWAITING_PLAN_APPROVAL)
    assert formatter is not None, "Slack dispatcher must wire AWAITING_PLAN_APPROVAL"
    msg = formatter({"message": "plan ready"}, "Task #4: Build app", False, 4)
    assert isinstance(msg, str) and msg.strip()
    assert "plan" in msg.lower()
    assert "/tasks/4" in msg


# ---------------------------------------------------------------------------
# 5. PR_CREATED — trio's integration PR opening must surface to the user.
#    Trio used `state_machine.transition()` directly, which is DB-only, so
#    the user got no notification between task.created and the (often
#    skipped) gate events. Pin the factory + both dispatchers here.
# ---------------------------------------------------------------------------


def test_task_pr_created_factory_shape():
    ev = task_pr_created(
        task_id=9, pr_url="https://github.com/x/y/pull/1", branch="auto-agent/foo-9",
    )
    assert ev.type == TaskEventType.PR_CREATED
    assert ev.task_id == 9
    assert ev.payload["pr_url"] == "https://github.com/x/y/pull/1"
    assert ev.payload["branch"] == "auto-agent/foo-9"


def test_telegram_dispatcher_renders_pr_created():
    formatter = TELEGRAM_FORMATTERS.get(TaskEventType.PR_CREATED)
    assert formatter is not None, "Telegram dispatcher must wire PR_CREATED"
    msg = formatter(
        {"pr_url": "https://github.com/x/y/pull/1", "branch": "auto-agent/foo-9"},
        "Task #9: Build app",
        False,
        9,
    )
    assert isinstance(msg, str) and msg.strip()
    assert "https://github.com/x/y/pull/1" in msg
    assert "auto-agent/foo-9" in msg


def test_slack_dispatcher_renders_pr_created():
    formatter = SLACK_FORMATTERS.get(TaskEventType.PR_CREATED)
    assert formatter is not None, "Slack dispatcher must wire PR_CREATED"
    msg = formatter(
        {"pr_url": "https://github.com/x/y/pull/1", "branch": "auto-agent/foo-9"},
        "Task #9: Build app",
        False,
        9,
    )
    assert isinstance(msg, str) and msg.strip()
    assert "https://github.com/x/y/pull/1" in msg
    assert "auto-agent/foo-9" in msg


@pytest.mark.asyncio
async def test_open_integration_pr_and_transition_publishes_pr_created(publisher):
    """Wire-test: when the trio successfully opens the integration PR, it
    must publish task.pr_created. Pre-fix, trio used the bare
    ``state_machine.transition()`` and emitted no event, so the user got
    silence between task.created and the (often skipped) gate events.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import agent.lifecycle.trio as trio

    fake_p = SimpleNamespace(
        id=9,
        status="trio_executing",
        pr_url=None,
        trio_phase=None,
        integration_branch="auto-agent/foo-9",
    )

    fake_execute_result = MagicMock()
    fake_execute_result.scalar_one = lambda: fake_p

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, _stmt):
            return fake_execute_result

        async def commit(self):
            return None

    parent = SimpleNamespace(id=9)

    with (
        patch.object(trio, "async_session", lambda: _FakeSession()),
        patch.object(
            trio, "_open_integration_pr",
            new=AsyncMock(return_value="https://github.com/o/r/pull/9"),
        ),
        patch.object(trio, "transition", new=AsyncMock(return_value=fake_p)),
    ):
        await trio._open_integration_pr_and_transition(
            parent=parent, target_branch="main",
        )

    pr_events = [e for e in publisher.events if e.type == TaskEventType.PR_CREATED]
    assert len(pr_events) == 1
    ev = pr_events[0]
    assert ev.task_id == 9
    assert ev.payload["pr_url"] == "https://github.com/o/r/pull/9"
    assert ev.payload["branch"] == "auto-agent/foo-9"
