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

# (event type, dotted path of the handler register_handlers should wire it to).
# Adding a new lifecycle event? Add a row here and to register_handlers — the
# parametrized test below catches the regression of registering the wrong
# handler or forgetting one entirely.
EVENT_TO_HANDLER = [
    ("task.start_planning", "agent.lifecycle.planning.handle"),
    ("task.plan_ready", "agent.lifecycle.review.handle_plan_ready"),
    ("task.start_coding", "agent.lifecycle.coding.handle"),
    ("task.deploy_preview", "agent.lifecycle.deploy.handle"),
    ("task.query", "agent.lifecycle.query.handle"),
    ("task.cleanup", "agent.lifecycle.cleanup.handle"),
    ("task.clarification_response", "agent.lifecycle.conversation.handle_clarification_event"),
    ("po.analyze", "agent.lifecycle.po_worker.handle"),
    ("repo.onboard", "agent.lifecycle.harness_onboard.handle"),
    ("human.message", "agent.lifecycle.conversation.route_human_message"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type,handler_path", EVENT_TO_HANDLER)
async def test_dispatch_routes_each_event_to_correct_handler(event_type, handler_path):
    """Each event type lands on the handler register_handlers wired up.

    Doubles as the "every event type has a handler" check — if a row is added
    here without a matching register_handlers entry, the mock won't be awaited.
    """
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
