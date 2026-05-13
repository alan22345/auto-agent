"""Regression — verify intent layer.

A diff that does not address the task must NOT reach PR_CREATED. Two consecutive
intent-check failures land the task in BLOCKED with the PR-creation code path
never reached. Maps to acceptance criterion #1 in the spec (intent layer).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from agent.lifecycle import verify
from shared.types import IntentVerdict


async def test_intent_mismatch_blocks_with_no_pr_created(monkeypatch):
    task = MagicMock(
        id=9002,
        title="add dark mode toggle",
        description="add a dark mode toggle to /settings",
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
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "agent.lifecycle.verify.publish", AsyncMock(),
    )
    monkeypatch.setattr(
        "agent.tools.dev_server.sniff_run_command",
        lambda ws, override=None: None,  # no runner → boot skipped
    )

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

    # Stub intent check to NOT-OK on both cycles.
    monkeypatch.setattr(
        "agent.lifecycle.verify.run_intent_check",
        AsyncMock(return_value=IntentVerdict(
            ok=False,
            reasoning="no dark-mode toggle found in diff",
            tool_calls=[],
        )),
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

    # Cycle 1 → coding.
    await verify.handle_verify(9002)
    # Cycle 2 → blocked.
    await verify.handle_verify(9002)

    pr_helper.assert_not_called()
    statuses = [t[1] for t in transitions]
    assert "coding" in statuses
    assert statuses[-1] == "blocked"
