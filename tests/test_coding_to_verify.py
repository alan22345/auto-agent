"""Test that _finish_coding transitions to VERIFYING and dispatches handle_verify."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.lifecycle import coding


@pytest.mark.asyncio
async def test_finish_coding_dispatches_verify(monkeypatch):
    monkeypatch.setattr("agent.lifecycle.coding.transition_task", AsyncMock())
    monkeypatch.setattr(
        "agent.lifecycle.coding.commit_pending_changes", AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "agent.lifecycle.coding.ensure_branch_has_commits", AsyncMock(),
    )
    monkeypatch.setattr("agent.lifecycle.coding.push_branch", AsyncMock())

    # Stub create_agent so the self-review agent run returns REVIEW_PASSED
    mock_result = MagicMock()
    mock_result.output = "REVIEW_PASSED"
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=mock_result)
    monkeypatch.setattr(
        "agent.lifecycle.coding.create_agent",
        lambda *a, **kw: mock_agent,
    )
    monkeypatch.setattr(
        "agent.lifecycle.coding.home_dir_for_task",
        AsyncMock(return_value=None),
    )

    mock_verify = AsyncMock()
    # Patch the verify module that coding imports lazily
    import agent.lifecycle.verify as verify_mod
    monkeypatch.setattr(verify_mod, "handle_verify", mock_verify)

    task = MagicMock()
    task.id = 42
    task.title = "t"
    task.description = "d"
    task.affected_routes = []
    task.created_by_user_id = None
    task.organization_id = 1

    transition_mock = AsyncMock()
    monkeypatch.setattr("agent.lifecycle.coding.transition_task", transition_mock)

    await coding._finish_coding(
        task_id=42, task=task, workspace="/tmp/ws", session_id="s",
        base_branch="main", branch_name="b",
    )

    # Must have transitioned to verifying
    transition_mock.assert_called_once_with(
        42, "verifying", "self-review complete; dispatching verify"
    )
    # Must have dispatched handle_verify
    mock_verify.assert_called_once_with(42)
