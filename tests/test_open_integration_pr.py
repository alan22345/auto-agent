"""Tests for ``_open_integration_pr`` and the failure-path transition —
ADR-015 Phase 7.7.

The production fix has three load-bearing properties:

1. ``git push -u origin <branch>`` runs BEFORE ``gh pr create``, both with
   ``cwd=workspace``. Prior code skipped the push and ran ``gh pr create``
   with no cwd, so production hit "fatal: not a git repository" and lost
   the integration branch.
2. On push or PR-create failure ``_open_integration_pr`` raises so the
   caller can transition the parent to BLOCKED instead of silently
   transitioning to PR_CREATED.
3. The branch name is the new ``auto-agent/<slug>-<id>`` (or the legacy
   ``trio/<id>`` for in-flight tasks whose ``Task.integration_branch``
   column is NULL).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import agent.lifecycle.trio as trio


def _fake_parent(
    *,
    id: int = 7,
    title: str = "Parallel Universe screen",
    integration_branch: str | None = "auto-agent/parallel-universe-screen-7",
):
    """Tiny stand-in for a Task ORM row that satisfies _open_integration_pr."""
    return SimpleNamespace(
        id=id,
        title=title,
        created_by_user_id=None,
        organization_id=1,
        integration_branch=integration_branch,
    )


class _Result:
    """Minimal stand-in for ``agent.sh.RunResult``."""

    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    @property
    def failed(self) -> bool:
        return self.returncode != 0


@pytest.mark.asyncio
async def test_open_integration_pr_pushes_then_creates_with_workspace_cwd():
    """git push must precede gh pr create; both run from the workspace dir."""

    parent = _fake_parent()
    calls: list[tuple[tuple, dict]] = []

    async def fake_run(argv, *, cwd=None, timeout=None, env=None, **_kw):
        calls.append((tuple(argv), {"cwd": cwd, "timeout": timeout, "env": env}))
        if argv[:2] == ["git", "push"]:
            return _Result(stdout="", stderr="")
        if argv[:3] == ["gh", "pr", "create"]:
            return _Result(stdout="https://github.com/o/r/pull/9\n")
        return _Result()

    with (
        patch(
            "agent.lifecycle.trio.architect._prepare_parent_workspace",
            new=AsyncMock(return_value="/tmp/ws/7"),
        ),
        patch(
            "shared.github_auth.get_github_token",
            new=AsyncMock(return_value="ghp_x"),
        ),
        patch("agent.sh.run", new=fake_run),
    ):
        url = await trio._open_integration_pr(parent, "main")

    assert url == "https://github.com/o/r/pull/9"

    # The strip step ("git ls-files .auto-agent") may fire before push — it's
    # a precondition that runs but is a no-op when nothing is tracked. Filter
    # it out so this test focuses on the push → PR ordering.
    relevant = [
        (argv, kw) for argv, kw in calls if argv[0:2] not in (("git", "ls-files"),)
    ]

    # git push fires first, with the workspace cwd and the branch name.
    push_call, push_kw = relevant[0]
    assert push_call[0:2] == ("git", "push")
    assert "auto-agent/parallel-universe-screen-7" in push_call
    assert push_kw["cwd"] == "/tmp/ws/7"

    # gh pr create fires after, also with the workspace cwd.
    pr_call, pr_kw = relevant[1]
    assert pr_call[0:3] == ("gh", "pr", "create")
    assert pr_kw["cwd"] == "/tmp/ws/7"
    assert "--head" in pr_call
    head_idx = pr_call.index("--head")
    assert pr_call[head_idx + 1] == "auto-agent/parallel-universe-screen-7"
    base_idx = pr_call.index("--base")
    assert pr_call[base_idx + 1] == "main"


@pytest.mark.asyncio
async def test_open_integration_pr_raises_when_push_fails():
    """A failed push must NOT proceed to PR creation; caller transitions to BLOCKED."""

    parent = _fake_parent()
    pr_create_called = False

    async def fake_run(argv, *, cwd=None, timeout=None, env=None, **_kw):
        nonlocal pr_create_called
        if argv[:2] == ["git", "push"]:
            return _Result(stderr="fatal: cannot push", returncode=128)
        if argv[:3] == ["gh", "pr", "create"]:
            pr_create_called = True
            return _Result(stdout="should not happen")
        return _Result()

    with (
        patch(
            "agent.lifecycle.trio.architect._prepare_parent_workspace",
            new=AsyncMock(return_value="/tmp/ws/7"),
        ),
        patch(
            "shared.github_auth.get_github_token",
            new=AsyncMock(return_value="ghp_x"),
        ),
        patch("agent.sh.run", new=fake_run),
        pytest.raises(RuntimeError),
    ):
        await trio._open_integration_pr(parent, "main")

    assert pr_create_called is False, "gh pr create must not run when push fails"


@pytest.mark.asyncio
async def test_open_integration_pr_raises_when_gh_create_fails():
    """A failed gh pr create must raise so the caller does NOT transition to PR_CREATED."""

    parent = _fake_parent()

    async def fake_run(argv, *, cwd=None, timeout=None, env=None, **_kw):
        if argv[:2] == ["git", "push"]:
            return _Result()
        if argv[:3] == ["gh", "pr", "create"]:
            return _Result(stderr="HTTP 422: validation failed", returncode=1)
        return _Result()

    with (
        patch(
            "agent.lifecycle.trio.architect._prepare_parent_workspace",
            new=AsyncMock(return_value="/tmp/ws/7"),
        ),
        patch(
            "shared.github_auth.get_github_token",
            new=AsyncMock(return_value="ghp_x"),
        ),
        patch("agent.sh.run", new=fake_run),
        pytest.raises(RuntimeError),
    ):
        await trio._open_integration_pr(parent, "main")


@pytest.mark.asyncio
async def test_open_integration_pr_uses_legacy_branch_when_column_null():
    """In-flight tasks without ``integration_branch`` keep the legacy ``trio/<id>`` name."""

    parent = _fake_parent(integration_branch=None, id=1, title="task 1")
    captured_argvs: list[list[str]] = []

    async def fake_run(argv, *, cwd=None, timeout=None, env=None, **_kw):
        captured_argvs.append(list(argv))
        if argv[:3] == ["gh", "pr", "create"]:
            return _Result(stdout="https://github.com/o/r/pull/1\n")
        return _Result()

    with (
        patch(
            "agent.lifecycle.trio.architect._prepare_parent_workspace",
            new=AsyncMock(return_value="/tmp/ws/1"),
        ),
        patch(
            "shared.github_auth.get_github_token",
            new=AsyncMock(return_value="ghp_x"),
        ),
        patch("agent.sh.run", new=fake_run),
    ):
        await trio._open_integration_pr(parent, "main")

    # Both the push and the gh pr create reference the legacy trio/1 branch.
    push = next(a for a in captured_argvs if a[:2] == ["git", "push"])
    pr = next(a for a in captured_argvs if a[:3] == ["gh", "pr", "create"])
    assert "trio/1" in push
    assert "trio/1" in pr
