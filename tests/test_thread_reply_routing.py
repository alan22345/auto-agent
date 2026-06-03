"""Thread-reply feedback must reach the task via the event bus.

Regression for the 2026-06-03 bug: Slack/Telegram thread replies POSTed to the
org-scoped ``/tasks/{id}/messages`` endpoint with only an ``X-Sender`` header
and no auth, so ``current_org_id`` raised 401 "Not authenticated". The bridge's
``_post_task_feedback`` called ``httpx.post`` without ``raise_for_status`` inside
a transport-only ``except``, so the 401 was silently swallowed while the bot
still confirmed "Sent to task #N". The clarification answer was dropped and the
task stayed stuck in AWAITING_CLARIFICATION.

The fix routes the reply as a ``human.message`` event directly on the in-process
bus (no HTTP, no auth) so ``route_human_message`` -> ``handle_clarification_inbound``
picks it up — mirroring the messenger_router thread fast-path.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from shared.events import HumanEventType

pytestmark = pytest.mark.asyncio


def _stub_aiohttp() -> None:
    """Stub aiohttp + slack socket-mode adapters so importing
    integrations.slack.main works in the test venv (aiohttp is only installed
    in the deployed image). Mirrors tests/test_slack_multi_team_routing.py.
    """

    class _AiohttpStub(ModuleType):
        def __getattr__(self, name: str):
            obj = MagicMock()
            setattr(self, name, obj)
            return obj

    stub = _AiohttpStub("aiohttp")
    sys.modules["aiohttp"] = stub
    for name in ("FormData", "BasicAuth", "ClientSession", "web", "TCPConnector"):
        setattr(stub, name, MagicMock())
    for mod_name in (
        "slack_sdk.socket_mode.aiohttp",
        "slack_bolt.adapter.socket_mode.aiohttp",
        "slack_bolt.adapter.socket_mode.async_handler",
        "slack_bolt.adapter.aiohttp",
    ):
        m = ModuleType(mod_name)
        m.AsyncSocketModeHandler = MagicMock()  # type: ignore[attr-defined]
        m.SocketModeClient = MagicMock()  # type: ignore[attr-defined]
        m.to_bolt_request = MagicMock()  # type: ignore[attr-defined]
        m.to_aiohttp_response = MagicMock()  # type: ignore[attr-defined]
        sys.modules[mod_name] = m


async def test_slack_post_task_feedback_publishes_human_message(publisher):
    _stub_aiohttp()
    from integrations.slack.main import _post_task_feedback

    await _post_task_feedback(41, "below 70 green, below 85 yellow", sender="slack:alice")

    human = [e for e in publisher.events if e.type == HumanEventType.MESSAGE]
    assert len(human) == 1
    ev = human[0]
    assert ev.task_id == 41
    assert ev.payload["message"] == "below 70 green, below 85 yellow"
    assert ev.payload["source"] == "slack"


async def test_telegram_post_task_feedback_publishes_human_message(publisher):
    from integrations.telegram.main import _post_task_feedback

    await _post_task_feedback(41, "only when paused", sender="telegram:bob")

    human = [e for e in publisher.events if e.type == HumanEventType.MESSAGE]
    assert len(human) == 1
    ev = human[0]
    assert ev.task_id == 41
    assert ev.payload["message"] == "only when paused"
    assert ev.payload["source"] == "telegram"
