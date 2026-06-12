"""Regression: clarification resume must target the ORG-SCOPED workspace.

Incident (task #327, 2026-06-12): a coding-phase clarification answer resumed
the agent against ``<WORKSPACES_DIR>/task-327`` while the coding phase had
cloned into ``<WORKSPACES_DIR>/<org_id>/task-327`` (per ``_workspace_path``).
The non-existent ``cwd`` made the ``claude`` subprocess raise
``FileNotFoundError`` on every resume attempt, stranding the task in
AWAITING_CLARIFICATION. The resume must use the same workspace resolver the
coding phase uses.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.workspace import _workspace_path


@pytest.mark.asyncio
async def test_clarification_resume_uses_org_scoped_workspace():
    task = SimpleNamespace(
        id=327,
        organization_id=9,
        repo_name="ergodic-ai/causal-tool-data-generator",
        created_at=dt.datetime(2026, 6, 12, tzinfo=dt.UTC),
        intake_qa=None,
        plan="some plan",  # not grill phase
        description="d",
        created_by_user_id=1,
        branch_name=None,
    )
    repo = SimpleNamespace(
        id=26, name="causal-tool-data-generator",
        url="https://github.com/ergodic-ai/causal-tool-data-generator.git",
        default_branch="main",
    )
    expected = _workspace_path(task_id=327, organization_id=9)

    agent = MagicMock()
    agent.run = AsyncMock(return_value=SimpleNamespace(output=""))
    create_agent = MagicMock(return_value=agent)
    # clone_repo returns the org-scoped workspace path (and materialises it on
    # disk) — mock it so the test does no real git, but assert it was invoked.
    clone_repo = AsyncMock(return_value=expected)

    with (
        patch("agent.lifecycle.conversation.get_task", AsyncMock(return_value=task)),
        patch("agent.lifecycle.conversation.get_repo", AsyncMock(return_value=repo)),
        patch("agent.lifecycle.conversation.home_dir_for_task", AsyncMock(return_value=None)),
        patch("agent.lifecycle.conversation.clone_repo", clone_repo),
        patch("agent.lifecycle.conversation.create_agent", create_agent),
        patch("agent.lifecycle.conversation.publish", AsyncMock()),
    ):
        from agent.lifecycle.conversation import handle_clarification_response
        await handle_clarification_response(327, "yes exactly I want A")

    # The resume must materialise the workspace, not assume it exists.
    clone_repo.assert_awaited_once()
    assert clone_repo.await_args.kwargs["organization_id"] == 9
    # ...and run the agent in that org-scoped workspace.
    create_agent.assert_called_once()
    passed_workspace = create_agent.call_args.args[0]
    assert passed_workspace == expected, (
        f"resume ran in {passed_workspace!r}, expected {expected!r}"
    )
