"""ADR-017 — Slack notifier renders task.iteration_complete."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock


class _AiohttpStub(ModuleType):
    def __getattr__(self, name):
        obj = MagicMock()
        setattr(self, name, obj)
        return obj


_aiohttp_stub = _AiohttpStub("aiohttp")
sys.modules["aiohttp"] = _aiohttp_stub
for _name in ("FormData", "BasicAuth", "ClientSession", "web", "TCPConnector"):
    setattr(_aiohttp_stub, _name, MagicMock())

for _mod_name in (
    "slack_sdk.socket_mode.aiohttp",
    "slack_bolt.adapter.socket_mode.aiohttp",
    "slack_bolt.adapter.socket_mode.async_handler",
    "slack_bolt.adapter.aiohttp",
):
    _stub = ModuleType(_mod_name)
    _stub.AsyncSocketModeHandler = MagicMock()  # type: ignore[attr-defined]
    _stub.SocketModeClient = MagicMock()  # type: ignore[attr-defined]
    _stub.to_bolt_request = MagicMock()  # type: ignore[attr-defined]
    _stub.to_aiohttp_response = MagicMock()  # type: ignore[attr-defined]
    sys.modules[_mod_name] = _stub


def test_iteration_complete_renders_in_slack():
    from integrations.slack.main import _NOTIFICATION_FORMATTERS
    from shared.events import TaskEventType

    assert TaskEventType.ITERATION_COMPLETE in _NOTIFICATION_FORMATTERS
    fmt = _NOTIFICATION_FORMATTERS[TaskEventType.ITERATION_COMPLETE]
    msg = fmt({"summary": "updated PR with your changes"}, "task info", False, 42)
    assert "updated PR" in msg.lower() or "iteration" in msg.lower()
