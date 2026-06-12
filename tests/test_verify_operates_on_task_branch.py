"""Regression: the coder's branch must survive coding→verify, and verify must
test that branch — not a base-reset workspace.

Incident (task #327): coding committed locally on the task branch but never
pushed; _finish_coding handed off to verify, whose _prepare_workspace re-ran
clone_repo on the same (ephemeral) workspace, which does `checkout <base>` +
`reset --hard origin/<base>` — switching HEAD off the coder's branch. The
commit was then unreachable from HEAD, so:
  - the intent check's `git diff <base>...HEAD` was empty every run, and
  - the PR push aborted with "Branch has no commits relative to 'main'".
Fix: push the branch at the end of coding (durable on origin), and have verify
check that branch out instead of base.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_prepare_workspace_checks_out_task_branch_not_base():
    from agent.lifecycle import verify

    task = SimpleNamespace(
        id=327, repo_name="ergodic-ai/causal-tool-data-generator",
        branch_name="auto-agent/data-gen-ui-327", freeform_mode=False,
        created_by_user_id=1, organization_id=9, repo_id=26,
    )
    repo = SimpleNamespace(
        id=26, url="https://github.com/ergodic-ai/causal-tool-data-generator.git",
        default_branch="main",
    )
    clone_repo = AsyncMock(return_value="/workspaces/9/task-327")

    with (
        patch("agent.lifecycle.verify.get_repo", AsyncMock(return_value=repo)),
        patch("agent.lifecycle.verify.clone_repo", clone_repo),
    ):
        workspace, base_branch = await verify._prepare_workspace(task)

    # Verify checks out the coder's branch (so it tests the real changes)...
    assert clone_repo.await_args.args[2] == "auto-agent/data-gen-ui-327"
    assert clone_repo.await_args.kwargs["fallback_branch"] == "main"
    # ...but still reports base for the diff/boot comparison.
    assert base_branch == "main"
    assert workspace == "/workspaces/9/task-327"


@pytest.mark.asyncio
async def test_finish_coding_pushes_branch_before_dispatching_verify():
    from agent.lifecycle import coding

    task = SimpleNamespace(
        id=327, title="data gen ui", description="d",
        organization_id=9, repo_id=26,
    )
    agent = MagicMock()
    agent.run = AsyncMock(return_value=SimpleNamespace(output="REVIEW_PASSED"))

    calls: list[str] = []
    push = AsyncMock(side_effect=lambda *a, **k: calls.append("push"))
    handle_verify = AsyncMock(side_effect=lambda *a, **k: calls.append("verify"))

    async def _transition(task_id, status, *a, **k):
        calls.append(f"transition:{status}")

    with (
        patch("agent.lifecycle.coding.create_agent", MagicMock(return_value=agent)),
        patch("agent.lifecycle.coding.home_dir_for_task", AsyncMock(return_value=None)),
        patch("agent.lifecycle.coding.commit_pending_changes", AsyncMock(return_value=False)),
        patch("agent.lifecycle.coding.push_branch", push),
        patch("agent.lifecycle.coding.transition_task", _transition),
        patch("agent.lifecycle.verify.handle_verify", handle_verify),
    ):
        await coding._finish_coding(
            327, task, "/workspaces/9/task-327", "sess",
            "main", "auto-agent/data-gen-ui-327",
        )

    assert "push" in calls, "branch must be pushed before verify"
    assert calls.index("push") < calls.index("transition:verifying") < calls.index("verify"), (
        f"push must precede the verify dispatch; got order {calls}"
    )
    push.assert_awaited_once()
    assert push.await_args.args[1] == "auto-agent/data-gen-ui-327"
