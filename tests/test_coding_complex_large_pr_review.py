"""complex_large taking the artefact-scope PR-reviewer path — ADR-015 §5 / Phase 7.

After Phase 7, ``_open_pr_and_advance`` should treat ``complex_large``
identically to ``complex`` for the self-PR-review step (artefact
scope): run the PR reviewer, address own comments, transition to DONE.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle import coding


@pytest.mark.asyncio
async def test_complex_large_uses_artefact_scope_pr_review() -> None:
    """A non-trio-child complex_large task runs _run_complex_pr_review."""

    task = SimpleNamespace(
        id=42,
        title="big thing",
        description="...",
        complexity="complex_large",
        parent_task_id=None,
        created_by_user_id=1,
        organization_id=1,
        base_branch="main",
    )

    captured: list[str] = []

    async def fake_complex_review(task_id, task, workspace, pr_url, base_branch):
        captured.append("complex_large_path")

    async def fake_simple_review(task_id, task, workspace, pr_url, base_branch):
        captured.append("simple_path")

    async def fake_independent_review(task_id, pr_url, branch_name):
        captured.append("independent_path")

    with (
        patch.object(coding, "commit_pending_changes", AsyncMock(return_value=False)),
        patch.object(coding, "ensure_branch_has_commits", AsyncMock(return_value=None)),
        patch.object(coding, "push_branch", AsyncMock(return_value=None)),
        patch.object(
            coding.review,
            "create_pr",
            AsyncMock(return_value="https://github.com/x/y/pull/1"),
        ),
        patch.object(coding, "_pr_title", AsyncMock(return_value="title")),
        patch.object(coding, "_run_complex_pr_review", fake_complex_review),
        patch.object(coding, "_run_simple_pr_review", fake_simple_review),
        patch.object(coding.review, "handle_independent_review", fake_independent_review),
    ):
        await coding._open_pr_and_advance(task.id, task, "/tmp/ws", "main", "feat/x")

    assert captured == ["complex_large_path"], captured
