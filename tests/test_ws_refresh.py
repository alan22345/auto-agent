"""Regression tests for the legacy ``web/`` UI's task-list refresh path.

The frontend used to send ``{ type: "get_tasks" }`` on
``task.subtask_progress`` events, but ``web/main.py`` only handles
``{ type: "refresh" }`` — so the message was silently dropped and the
subtask progress bars/counts went stale until an unrelated event
triggered a refresh.

Two complementary tests:

1. ``test_ws_refresh_returns_task_list`` exercises the server-side
   ``refresh`` handler end-to-end via the FastAPI WebSocket TestClient.
2. ``test_index_html_uses_refresh_for_subtask_progress`` is a static
   guard that reads ``web/static/index.html`` and asserts the orphan
   ``get_tasks`` string is gone and the subtask_progress branch sends
   ``refresh`` instead — this is the regression test that would have
   caught the bug.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from orchestrator.auth import create_token
from orchestrator.router import COOKIE_NAME
from web.main import app

_USER_ID = 42
_USERNAME = "ws_refresh_test_user"


def _valid_token() -> str:
    return create_token(_USER_ID, _USERNAME)


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


def test_index_html_uses_refresh_for_subtask_progress():
    """The legacy SPA must not emit the orphan ``get_tasks`` message.

    Asserts that:
    - The string ``type: "get_tasks"`` is absent from index.html (no
      handler exists for it in web/main.py).
    - The ``task.subtask_progress`` branch sends ``type: "refresh"``,
      which IS handled server-side.
    """
    index_path = Path(__file__).resolve().parent.parent / "web" / "static" / "index.html"
    text = index_path.read_text()

    assert 'type: "get_tasks"' not in text, (
        "web/static/index.html still contains an orphan get_tasks WS message; "
        "web/main.py has no handler for it — use 'refresh' instead."
    )

    marker = 'data.event_type === "task.subtask_progress"'
    idx = text.find(marker)
    assert idx != -1, "could not locate the task.subtask_progress branch"
    # Inspect the next ~400 chars (the small block that fires the WS send)
    block = text[idx : idx + 400]
    assert 'type: "refresh"' in block, (
        "subtask_progress branch must send {type: 'refresh'} so the server "
        "responds with a fresh task_list"
    )
