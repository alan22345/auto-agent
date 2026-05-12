"""Tests for dev-server lifecycle in the coding phase."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.lifecycle import coding


@pytest.mark.asyncio
async def test_coding_starts_server_when_routes_and_runner(monkeypatch):
    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(port=12345, log_path="/tmp/x.log", process=MagicMock(returncode=None))

    monkeypatch.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    monkeypatch.setattr(
        "agent.tools.dev_server.sniff_run_command",
        lambda ws, override=None: "npm run dev",
    )
    monkeypatch.setattr("agent.tools.dev_server.wait_for_port", AsyncMock())
    monkeypatch.setattr("agent.lifecycle.coding.get_freeform_config", AsyncMock(return_value=None))

    task = MagicMock(
        affected_routes=[{"path": "/", "label": "home"}],
        freeform_mode=True, repo_name="r",
    )
    cm = await coding._maybe_start_coding_server(task, "/tmp/ws")
    assert cm is not None  # context manager returned


@pytest.mark.asyncio
async def test_coding_no_server_when_no_routes():
    task = MagicMock(affected_routes=[])
    cm = await coding._maybe_start_coding_server(task, "/tmp/ws")
    assert cm is None


@pytest.mark.asyncio
async def test_coding_no_server_when_no_runner(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.dev_server.sniff_run_command",
        lambda ws, override=None: None,
    )
    monkeypatch.setattr("agent.lifecycle.coding.get_freeform_config", AsyncMock(return_value=None))
    task = MagicMock(
        affected_routes=[{"path": "/", "label": "home"}],
        freeform_mode=True, repo_name="r",
    )
    cm = await coding._maybe_start_coding_server(task, "/tmp/ws")
    assert cm is None
