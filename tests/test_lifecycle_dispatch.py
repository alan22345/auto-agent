"""Tests for the lifecycle event-bus wiring in agent/main.py.

The Phase-9 refactor replaced a 100-line elif chain over event.type with
``register_handlers(bus)``. These tests catch future regressions where a
new event type is added to a lifecycle module but never registered (the
dispatcher would silently drop it), or where the wrong handler is wired.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.main import register_handlers
from shared.events import Event, EventBus

REGISTERED_EVENT_TYPES = [
    "task.start_planning",
    "task.plan_ready",
    "task.start_coding",
    "task.deploy_preview",
    "task.query",
    "task.cleanup",
    "task.clarification_response",
    "po.analyze",
    "repo.onboard",
    "human.message",
]


def test_register_handlers_registers_every_event_type():
    """Every event type the agent currently dispatches must have a handler."""
    bus = EventBus()
    register_handlers(bus)
    registered_patterns = {pattern for pattern, _ in bus._handlers}
    for ev_type in REGISTERED_EVENT_TYPES:
        assert ev_type in registered_patterns, (
            f"Missing handler registration for {ev_type!r}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type,handler_path",
    [
        ("task.start_planning", "agent.lifecycle.planning.handle"),
        ("task.plan_ready", "agent.lifecycle.review.handle_plan_ready"),
        ("task.start_coding", "agent.lifecycle.coding.handle"),
        ("task.deploy_preview", "agent.lifecycle.deploy.handle"),
        ("task.query", "agent.lifecycle.query.handle"),
        ("task.cleanup", "agent.lifecycle.cleanup.handle"),
        (
            "task.clarification_response",
            "agent.lifecycle.conversation.handle_clarification_event",
        ),
        ("po.analyze", "agent.lifecycle.po_worker.handle"),
        ("repo.onboard", "agent.lifecycle.harness_onboard.handle"),
        ("human.message", "agent.lifecycle.conversation.route_human_message"),
    ],
)
async def test_dispatch_routes_each_event_to_correct_handler(event_type, handler_path):
    """Each event type lands on the handler we said it would."""
    with patch(handler_path, new_callable=AsyncMock) as mock_handler:
        bus = EventBus()
        register_handlers(bus)
        event = Event(type=event_type, task_id=42, payload={"foo": "bar"})
        await bus.dispatch(event)

    mock_handler.assert_awaited_once()
    received = mock_handler.await_args.args[0]
    assert received.type == event_type
    assert received.task_id == 42


@pytest.mark.asyncio
async def test_dispatch_drops_unknown_event_types_silently():
    """An event with no registered pattern is a no-op (matches the old elif default)."""
    bus = EventBus()
    register_handlers(bus)
    # No handler for "task.unknown" — this MUST NOT raise
    await bus.dispatch(Event(type="task.unknown", task_id=1))
