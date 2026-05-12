"""Tests for agent/lifecycle/verify.py — Tasks 17 & 18."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.lifecycle import verify
from shared.types import IntentVerdict


@pytest.fixture
def patches(monkeypatch):
    """Patch verify.py's external dependencies."""
    monkeypatch.setattr("agent.lifecycle.verify.get_task", AsyncMock())
    monkeypatch.setattr("agent.lifecycle.verify.transition_task", AsyncMock())
    monkeypatch.setattr(
        "agent.lifecycle.verify._prepare_workspace",
        AsyncMock(return_value=("/tmp/ws", "main")),
    )
    monkeypatch.setattr(
        "agent.lifecycle.verify._resolve_run_command_override", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "agent.lifecycle.verify._create_verify_attempt",
        AsyncMock(return_value=MagicMock(id=1, cycle=1)),
    )
    monkeypatch.setattr("agent.lifecycle.verify._update_verify_attempt", AsyncMock())
    monkeypatch.setattr("agent.lifecycle.verify._next_cycle", AsyncMock(return_value=1))
    monkeypatch.setattr("agent.lifecycle.verify.publish", AsyncMock())
    return monkeypatch


async def test_boot_pass_intent_pass_invokes_open_pr(patches):
    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(port=12345, log_path="/tmp/log", process=MagicMock(returncode=None))

    patches.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    patches.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: "npm run dev")
    patches.setattr("agent.tools.dev_server.wait_for_port", AsyncMock())
    patches.setattr("agent.tools.dev_server.hold", AsyncMock())
    patches.setattr(
        "agent.lifecycle.verify.run_intent_check",
        AsyncMock(return_value=IntentVerdict(ok=True, reasoning="ok", tool_calls=[])),
    )

    open_pr = AsyncMock()
    patches.setattr("agent.lifecycle.coding._open_pr_and_advance", open_pr)

    verify.get_task.return_value = MagicMock(
        id=42, affected_routes=[], freeform_mode=True, repo_name="r", branch_name="b",
    )
    await verify.handle_verify(42)
    open_pr.assert_called_once()


async def test_no_runner_intent_only_passes(patches):
    patches.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: None)
    patches.setattr(
        "agent.lifecycle.verify.run_intent_check",
        AsyncMock(return_value=IntentVerdict(ok=True, reasoning="ok", tool_calls=[])),
    )
    open_pr = AsyncMock()
    patches.setattr("agent.lifecycle.coding._open_pr_and_advance", open_pr)

    verify.get_task.return_value = MagicMock(
        id=42, affected_routes=[], freeform_mode=True, repo_name="r", branch_name="b",
    )
    await verify.handle_verify(42)
    open_pr.assert_called_once()


async def test_early_exit_loops_back_to_coding(patches):
    from agent.tools.dev_server import EarlyExit

    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(port=12345, log_path="/tmp/log", process=MagicMock(returncode=None))

    patches.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    patches.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: "npm run dev")
    patches.setattr("agent.tools.dev_server.wait_for_port", AsyncMock())
    patches.setattr("agent.tools.dev_server.hold", AsyncMock(side_effect=EarlyExit("crashed")))

    open_pr = AsyncMock()
    patches.setattr("agent.lifecycle.coding._open_pr_and_advance", open_pr)

    verify.get_task.return_value = MagicMock(
        id=42, affected_routes=[], freeform_mode=True, repo_name="r", branch_name="b",
    )
    await verify.handle_verify(42)
    open_pr.assert_not_called()
    # Last transition went to "coding" (cycle 1 fail).
    from unittest.mock import ANY
    verify.transition_task.assert_called_with(42, "coding", ANY)


async def test_intent_fail_loops_back(patches):
    patches.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: None)
    patches.setattr(
        "agent.lifecycle.verify.run_intent_check",
        AsyncMock(return_value=IntentVerdict(ok=False, reasoning="missing toggle", tool_calls=[])),
    )
    open_pr = AsyncMock()
    patches.setattr("agent.lifecycle.coding._open_pr_and_advance", open_pr)

    verify.get_task.return_value = MagicMock(
        id=42, affected_routes=[], freeform_mode=True, repo_name="r", branch_name="b",
    )
    await verify.handle_verify(42)
    open_pr.assert_not_called()
    from unittest.mock import ANY
    verify.transition_task.assert_called_with(42, "coding", ANY)


async def test_second_failure_blocks(patches):
    patches.setattr("agent.lifecycle.verify._next_cycle", AsyncMock(return_value=2))
    patches.setattr("agent.tools.dev_server.sniff_run_command", lambda ws, override=None: None)
    patches.setattr(
        "agent.lifecycle.verify.run_intent_check",
        AsyncMock(return_value=IntentVerdict(ok=False, reasoning="still wrong", tool_calls=[])),
    )
    # Recreate the attempt mock with cycle=2
    patches.setattr(
        "agent.lifecycle.verify._create_verify_attempt",
        AsyncMock(return_value=MagicMock(id=2, cycle=2)),
    )

    verify.get_task.return_value = MagicMock(
        id=42, affected_routes=[], freeform_mode=True, repo_name="r", branch_name="b",
    )
    await verify.handle_verify(42)
    from unittest.mock import ANY
    verify.transition_task.assert_called_with(42, "blocked", ANY)


async def test_intent_check_uses_browse_url_when_server_running(patches, monkeypatch):
    """T18: Spy on create_agent to confirm with_browser=True is passed when server is running."""
    captured = {}

    def fake_create_agent(workspace, *, with_browser=False, dev_server_log_path=None, **kw):
        captured["with_browser"] = with_browser
        captured["dev_server_log_path"] = dev_server_log_path
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(
            return_value=MagicMock(output="OK\nLooks good.", tool_calls=[])
        )
        return mock_agent

    monkeypatch.setattr("agent.lifecycle.verify.create_agent", fake_create_agent)
    monkeypatch.setattr("agent.lifecycle.verify._fresh_session_id", lambda *a, **kw: "session-xyz")
    monkeypatch.setattr("agent.lifecycle.verify.home_dir_for_task", AsyncMock(return_value="/tmp/home"))

    # Mock sh.run to return a fake diff stat
    sh_mock = MagicMock()
    sh_mock.run = AsyncMock(return_value=MagicMock(stdout="x | 1 +", stderr="", returncode=0, timed_out=False))
    monkeypatch.setattr("agent.lifecycle.verify.sh", sh_mock)

    server = MagicMock(port=12345, log_path="/tmp/log")

    class FakeTask:
        id = 1
        title = "t"
        description = "d"
        repo_name = "r"
        organization_id = 1

        def __init__(self):
            self.affected_routes = [{"path": "/", "label": "home"}]

    verdict = await verify.run_intent_check(FakeTask(), "/tmp/ws", server)
    assert verdict.ok is True
    assert captured["with_browser"] is True
    assert captured["dev_server_log_path"] == "/tmp/log"


async def test_intent_check_fails_closed_on_hedged_output(patches, monkeypatch):
    """Output like 'OK on routes, but missing toggle' must be treated as NOT-OK."""
    def fake_create_agent(workspace, **kw):
        return MagicMock(run=AsyncMock(return_value=MagicMock(
            output="OK on routes, but missing toggle", tool_calls=[],
        )))

    monkeypatch.setattr("agent.lifecycle.verify.create_agent", fake_create_agent)
    monkeypatch.setattr("agent.lifecycle.verify._fresh_session_id", lambda *a, **kw: "session-xyz")
    monkeypatch.setattr("agent.lifecycle.verify.home_dir_for_task", AsyncMock(return_value="/tmp/home"))
    sh_mock = MagicMock()
    sh_mock.run = AsyncMock(return_value=MagicMock(stdout="x | 1 +", stderr=""))
    monkeypatch.setattr("agent.lifecycle.verify.sh", sh_mock, raising=False)

    server = MagicMock(port=12345, log_path="/tmp/log")
    verdict = await verify.run_intent_check(
        type("T", (), {"id": 1, "title": "t", "description": "d", "affected_routes": [], "repo_name": "r", "organization_id": 1})(),
        "/tmp/ws",
        server,
    )
    assert verdict.ok is False


async def test_intent_check_passes_on_exact_ok(patches, monkeypatch):
    def fake_create_agent(workspace, **kw):
        return MagicMock(run=AsyncMock(return_value=MagicMock(
            output="OK\nLooks good.", tool_calls=[],
        )))

    monkeypatch.setattr("agent.lifecycle.verify.create_agent", fake_create_agent)
    monkeypatch.setattr("agent.lifecycle.verify._fresh_session_id", lambda *a, **kw: "session-xyz")
    monkeypatch.setattr("agent.lifecycle.verify.home_dir_for_task", AsyncMock(return_value="/tmp/home"))
    sh_mock = MagicMock()
    sh_mock.run = AsyncMock(return_value=MagicMock(stdout="x | 1 +", stderr=""))
    monkeypatch.setattr("agent.lifecycle.verify.sh", sh_mock, raising=False)

    server = MagicMock(port=12345, log_path="/tmp/log")
    verdict = await verify.run_intent_check(
        type("T", (), {"id": 1, "title": "t", "description": "d", "affected_routes": [], "repo_name": "r", "organization_id": 1})(),
        "/tmp/ws",
        server,
    )
    assert verdict.ok is True
