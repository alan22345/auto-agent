"""WebSocket auth: cookie (preferred) and query-param (legacy fallback).

Tests verify that:
1. A valid cookie grants access and the client receives an initial ``task_list`` message.
2. No cookie and no ``?token=`` causes the server to close with code 4001.

FastAPI's TestClient WebSocket helper does honour cookies set via
``client.cookies.set()``, so we use that path directly.  The ``task_list``
fetch goes through httpx to the orchestrator; we mock that call so the tests
stay self-contained.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from orchestrator.auth import create_token
from orchestrator.router import COOKIE_NAME

# ---------------------------------------------------------------------------
# Import the FastAPI app under test
# ---------------------------------------------------------------------------
# Importing web.main triggers a structlog / settings initialisation; that's
# fine for unit tests.
from web.main import app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USER_ID = 42
_USERNAME = "ws_test_user"


def _valid_token() -> str:
    return create_token(_USER_ID, _USERNAME, current_org_id=1)


def _mock_task_list_response():
    """Return a mock httpx response that pretends the orchestrator returned []."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = []
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_ws_cookie_auth_receives_task_list():
    """A WS connection that carries a valid session cookie gets task_list."""
    token = _valid_token()

    # Patch out the httpx call that fetches the initial task list
    with patch("web.main.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=_mock_task_list_response())
        mock_client_cls.return_value = mock_client

        client = TestClient(app, raise_server_exceptions=False)
        client.cookies.set(COOKIE_NAME, token)

        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "task_list"


def test_ws_no_auth_closes_4001():
    """A WS connection with no cookie and no ?token= is rejected with 4001."""
    client = TestClient(app, raise_server_exceptions=False)

    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect("/ws") as ws:
        # Server sends an error JSON then closes with 4001
        _msg = ws.receive_json()
        # Receive the disconnect (raises WebSocketDisconnect)
        ws.receive_text()
    assert exc_info.value.code == 4001
