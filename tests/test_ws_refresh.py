"""WebSocket ``refresh`` handler end-to-end test.

The Next.js UI sends ``{type: "refresh"}`` on ``task.subtask_progress``
events to keep subtask progress bars current; this test exercises the
server-side handler. The companion static-asset guard against the
decommissioned ``web/static/index.html`` SPA has been removed.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from orchestrator.auth import create_token
from orchestrator.router import COOKIE_NAME
from web.main import app

_USER_ID = 42
_USERNAME = "ws_refresh_test_user"


def _valid_token() -> str:
    return create_token(_USER_ID, _USERNAME, current_org_id=1)


def _mock_task_list_response():
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = []
    return resp


def test_ws_refresh_returns_task_list():
    """Sending ``{type: "refresh"}`` triggers a fresh ``task_list`` push."""
    token = _valid_token()

    with patch("web.main.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=_mock_task_list_response())
        mock_client_cls.return_value = mock_client

        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set(COOKIE_NAME, token)

        with client.websocket_connect("/ws") as ws:
            # Drain initial task_list pushed on connect
            initial = ws.receive_json()
            assert initial["type"] == "task_list"

            ws.send_json({"type": "refresh"})
            msg = ws.receive_json()
            assert msg["type"] == "task_list"
            assert msg["tasks"] == []


