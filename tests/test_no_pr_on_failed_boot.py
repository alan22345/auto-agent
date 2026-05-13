"""Regression — verify boot layer.

A task whose dev server cannot boot must NOT reach PR_CREATED. Two consecutive
boot-check failures land the task in BLOCKED with the PR-creation code path
never reached. Maps to acceptance criterion #1 in the spec (boot layer).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from agent.lifecycle import verify


async def test_failed_boot_blocks_with_no_pr_created(monkeypatch):
    task = MagicMock(
        id=9001,
        title="break the server",
        description="d",
        freeform_mode=True,
        created_by_user_id=None,
        organization_id=1,
        repo_name="r",
        branch_name="b",
        affected_routes=[],
    )

    monkeypatch.setattr(
        "agent.lifecycle.verify.get_task", AsyncMock(return_value=task),
    )
    monkeypatch.setattr(
        "agent.lifecycle.verify._prepare_workspace",
        AsyncMock(return_value=("/tmp/ws", "main")),
    )
    monkeypatch.setattr(
        "agent.lifecycle.verify._resolve_run_command_override",
        AsyncMock(return_value="node -e 'process.exit(1)'"),
    )
    monkeypatch.setattr(
        "agent.lifecycle.verify.publish", AsyncMock(),
    )

    # First call returns cycle 1, second returns cycle 2 — matches a real retry.
    monkeypatch.setattr(
        "agent.lifecycle.verify._next_cycle",
        AsyncMock(side_effect=[1, 2]),
    )
    monkeypatch.setattr(
        "agent.lifecycle.verify._create_verify_attempt",
        AsyncMock(side_effect=[MagicMock(id=1, cycle=1), MagicMock(id=2, cycle=2)]),
    )
    monkeypatch.setattr(
        "agent.lifecycle.verify._update_verify_attempt", AsyncMock(),
    )

    # Server "starts" but the hold-step trips EarlyExit on both cycles.
    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(port=12345, log_path="/tmp/log", process=MagicMock(returncode=1))

    monkeypatch.setattr(
        "agent.tools.dev_server.sniff_run_command",
        lambda ws, override=None: "node -e 'process.exit(1)'",
    )
    monkeypatch.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    monkeypatch.setattr("agent.tools.dev_server.wait_for_port", AsyncMock())
    from agent.tools.dev_server import EarlyExit
    monkeypatch.setattr(
        "agent.tools.dev_server.hold",
        AsyncMock(side_effect=EarlyExit("boom")),
    )

    pr_helper = AsyncMock()
    monkeypatch.setattr(
        "agent.lifecycle.coding._open_pr_and_advance", pr_helper,
    )

    transitions: list[tuple] = []

    async def fake_transition(task_id, status, message=""):
        transitions.append((task_id, status, message))

    monkeypatch.setattr(
        "agent.lifecycle.verify.transition_task", fake_transition,
    )

    # Cycle 1 — should loop back to coding.
    await verify.handle_verify(9001)
    # Cycle 2 — should block.
    await verify.handle_verify(9001)

    # PR creation must NEVER have been called.
    pr_helper.assert_not_called()
    # First transition went to coding; final transition went to blocked.
    statuses = [t[1] for t in transitions]
    assert "coding" in statuses
    assert statuses[-1] == "blocked"
