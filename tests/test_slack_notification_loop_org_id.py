# tests/test_slack_notification_loop_org_id.py
"""notification fan-out must pass task.organization_id to send_slack_dm
so the right per-workspace bot token is selected."""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub aiohttp + slack socket-mode adapters before importing the module.
# (Same pattern as tests/test_slack_multi_team_routing.py.)
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

# Safe to import now.
from integrations.slack import main as slack_main  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_TASK = {
    "id": 99,
    "title": "Test task",
    "freeform_mode": False,
    "organization_id": 42,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_created_event_routes_to_task_org():
    """_notify_task_event looks up task.organization_id and passes it to
    send_slack_dm so the per-org bot token is used."""
    send_mock = AsyncMock()

    async def fake_fetch(task_id: int) -> dict:
        return _FAKE_TASK

    async def fake_slack_user(task_id):
        return "U_OWNER"

    with (
        patch.object(slack_main, "send_slack_dm", new=send_mock),
        patch.object(slack_main, "_fetch_task_for_notification", new=fake_fetch),
        patch.object(slack_main, "_slack_user_id_for_task", new=fake_slack_user),
    ):
        await slack_main._notify_task_event(
            event_type="task.created",
            payload={"task_id": 99},
        )

    send_mock.assert_awaited_once()
    call_kwargs = send_mock.await_args.kwargs
    assert call_kwargs.get("org_id") == 42, (
        f"Expected org_id=42, got {call_kwargs.get('org_id')!r}"
    )


@pytest.mark.asyncio
async def test_task_event_without_task_id_uses_admin_path():
    """PO/system events (no task_id) fall through to the admin user path
    and do NOT pass org_id (it's not available for system events)."""
    send_mock = AsyncMock()

    with (
        patch.object(slack_main, "send_slack_dm", new=send_mock),
        patch.object(slack_main.settings, "slack_admin_user_id", "U_ADMIN"),
    ):
        await slack_main._notify_task_event(
            event_type="po.suggestions_ready",
            payload={"repo_name": "my-repo", "count": 3},
        )

    send_mock.assert_awaited_once()
    call_kwargs = send_mock.await_args.kwargs
    # org_id must be None for system events (no task to derive org from).
    assert call_kwargs.get("org_id") is None


@pytest.mark.asyncio
async def test_task_event_no_slack_user_skips_silently():
    """If the task owner hasn't linked Slack, send_slack_dm must not be
    called (prevents cryptic errors downstream)."""
    send_mock = AsyncMock()

    async def fake_fetch(task_id: int) -> dict:
        return _FAKE_TASK

    async def fake_slack_user(_task_id):
        return None  # not linked

    with (
        patch.object(slack_main, "send_slack_dm", new=send_mock),
        patch.object(slack_main, "_fetch_task_for_notification", new=fake_fetch),
        patch.object(slack_main, "_slack_user_id_for_task", new=fake_slack_user),
    ):
        await slack_main._notify_task_event(
            event_type="task.done",
            payload={"task_id": 99},
        )

    send_mock.assert_not_awaited()
