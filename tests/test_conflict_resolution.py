"""Tests for the merge-conflict resolver agent flow.

The resolver clones the feature branch, runs `git merge origin/<base>`, and
on a clean merge commits + pushes without invoking the agent. On conflicts
it invokes the agent to resolve, then commits + pushes. Failures emit
`task.merge_conflict_resolution_failed` with a reason.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import agent.conflict_resolver as cr


def _stub_task(task_id=11, repo_name="owner/repo", pr_url="https://github.com/owner/repo/pull/3"):
    class T:
        id = task_id

    t = T()
    t.repo_name = repo_name
    t.pr_url = pr_url
    return t


def _stub_repo(name="owner/repo", url="https://github.com/owner/repo.git"):
    class R:
        pass
    r = R()
    r.name = name
    r.url = url
    return r


def _published_types(captured):
    return [e.type for e in captured]


@pytest.mark.asyncio
async def test_resolver_no_task_emits_failed():
    captured = []

    async def fake_publish(event):
        captured.append(event)

    with patch("agent.conflict_resolver._get_task", AsyncMock(return_value=None)), \
         patch("agent.conflict_resolver.publish", side_effect=fake_publish):
        await cr.handle_merge_conflict_resolution(11, "https://github.com/owner/repo/pull/3")

    assert _published_types(captured) == ["task.merge_conflict_resolution_failed"]
    assert "task not found" in captured[0].payload["reason"]


@pytest.mark.asyncio
async def test_resolver_clean_merge_pushes_and_emits_success():
    task = _stub_task()
    repo = _stub_repo()
    git_calls: list[list[str]] = []

    async def fake_run_git(*args, cwd=None, check=False):
        git_calls.append(list(args))
        if args[:1] == ("fetch",):
            return ("", "", 0)
        if args[:1] == ("merge",):
            return ("Merge made by recursive strategy.\n", "", 0)
        if args[:1] == ("push",):
            return ("", "", 0)
        return ("", "", 0)

    async def fake_clone(*a, **kw):
        return "/tmp/ws"

    captured = []

    async def fake_publish(event):
        captured.append(event)

    agent_called = []

    async def fake_agent(*a, **k):
        agent_called.append(1)
        return True

    with patch("agent.conflict_resolver._get_task", AsyncMock(return_value=task)), \
         patch("agent.conflict_resolver._get_repo", AsyncMock(return_value=repo)), \
         patch("agent.conflict_resolver._fetch_pr_branches", AsyncMock(return_value=("feat/x", "main"))), \
         patch("agent.conflict_resolver.clone_repo", side_effect=fake_clone), \
         patch("agent.conflict_resolver._run_git", side_effect=fake_run_git), \
         patch("agent.conflict_resolver._run_agent_resolution", side_effect=fake_agent), \
         patch("agent.conflict_resolver.publish", side_effect=fake_publish):
        await cr.handle_merge_conflict_resolution(11, task.pr_url)

    assert agent_called == []
    assert any(c[:1] == ["push"] for c in git_calls)
    assert _published_types(captured) == ["task.merge_conflict_resolved"]
    assert captured[0].payload == {"head_branch": "feat/x"}


@pytest.mark.asyncio
async def test_resolver_conflicts_invoke_agent_then_succeed():
    task = _stub_task()
    repo = _stub_repo()
    git_calls: list[list[str]] = []

    async def fake_run_git(*args, cwd=None, check=False):
        git_calls.append(list(args))
        if args[:1] == ("fetch",):
            return ("", "", 0)
        if args[:1] == ("merge",) and len(args) > 1 and args[1].startswith("origin/"):
            return ("", "CONFLICT (content): Merge conflict in foo.py\n", 1)
        if args[:1] == ("diff",):
            return ("", "", 0)
        if args[:1] == ("commit",):
            return ("", "", 0)
        if args[:1] == ("push",):
            return ("", "", 0)
        return ("", "", 0)

    async def fake_clone(*a, **kw):
        return "/tmp/ws"

    async def fake_agent(workspace, base_branch, task_id):
        return True

    captured = []

    async def fake_publish(event):
        captured.append(event)

    with patch("agent.conflict_resolver._get_task", AsyncMock(return_value=task)), \
         patch("agent.conflict_resolver._get_repo", AsyncMock(return_value=repo)), \
         patch("agent.conflict_resolver._fetch_pr_branches", AsyncMock(return_value=("feat/x", "main"))), \
         patch("agent.conflict_resolver.clone_repo", side_effect=fake_clone), \
         patch("agent.conflict_resolver._run_git", side_effect=fake_run_git), \
         patch("agent.conflict_resolver._run_agent_resolution", side_effect=fake_agent), \
         patch("agent.conflict_resolver._has_conflict_markers", AsyncMock(return_value=False)), \
         patch("agent.conflict_resolver.publish", side_effect=fake_publish):
        await cr.handle_merge_conflict_resolution(11, task.pr_url)

    assert any(c[:2] == ["-c", "user.email=auto-agent@bot.local"] or c[0] == "commit" for c in git_calls)
    assert any(c[:1] == ["push"] for c in git_calls)
    assert _published_types(captured) == ["task.merge_conflict_resolved"]


@pytest.mark.asyncio
async def test_resolver_agent_giveup_emits_failed_and_aborts():
    task = _stub_task()
    repo = _stub_repo()
    git_calls: list[list[str]] = []

    async def fake_run_git(*args, cwd=None, check=False):
        git_calls.append(list(args))
        if args[:1] == ("fetch",):
            return ("", "", 0)
        if args[:1] == ("merge",) and len(args) > 1 and args[1].startswith("origin/"):
            return ("", "CONFLICT (content): Merge conflict in foo.py\n", 1)
        if args[:2] == ("merge", "--abort"):
            return ("", "", 0)
        return ("", "", 0)

    async def fake_clone(*a, **kw):
        return "/tmp/ws"

    async def fake_agent(workspace, base_branch, task_id):
        return False

    captured = []

    async def fake_publish(event):
        captured.append(event)

    with patch("agent.conflict_resolver._get_task", AsyncMock(return_value=task)), \
         patch("agent.conflict_resolver._get_repo", AsyncMock(return_value=repo)), \
         patch("agent.conflict_resolver._fetch_pr_branches", AsyncMock(return_value=("feat/x", "main"))), \
         patch("agent.conflict_resolver.clone_repo", side_effect=fake_clone), \
         patch("agent.conflict_resolver._run_git", side_effect=fake_run_git), \
         patch("agent.conflict_resolver._run_agent_resolution", side_effect=fake_agent), \
         patch("agent.conflict_resolver.publish", side_effect=fake_publish):
        await cr.handle_merge_conflict_resolution(11, task.pr_url)

    assert any(c[:2] == ["merge", "--abort"] for c in git_calls)
    assert _published_types(captured) == ["task.merge_conflict_resolution_failed"]


@pytest.mark.asyncio
async def test_resolver_remaining_conflict_markers_aborts():
    task = _stub_task()
    repo = _stub_repo()

    async def fake_run_git(*args, cwd=None, check=False):
        if args[:1] == ("merge",) and len(args) > 1 and args[1].startswith("origin/"):
            return ("", "CONFLICT (content)\n", 1)
        return ("", "", 0)

    async def fake_clone(*a, **kw):
        return "/tmp/ws"

    async def fake_agent(*a, **k):
        return True

    captured = []

    async def fake_publish(event):
        captured.append(event)

    with patch("agent.conflict_resolver._get_task", AsyncMock(return_value=task)), \
         patch("agent.conflict_resolver._get_repo", AsyncMock(return_value=repo)), \
         patch("agent.conflict_resolver._fetch_pr_branches", AsyncMock(return_value=("feat/x", "main"))), \
         patch("agent.conflict_resolver.clone_repo", side_effect=fake_clone), \
         patch("agent.conflict_resolver._run_git", side_effect=fake_run_git), \
         patch("agent.conflict_resolver._run_agent_resolution", side_effect=fake_agent), \
         patch("agent.conflict_resolver._has_conflict_markers", AsyncMock(return_value=True)), \
         patch("agent.conflict_resolver.publish", side_effect=fake_publish):
        await cr.handle_merge_conflict_resolution(11, task.pr_url)

    assert _published_types(captured) == ["task.merge_conflict_resolution_failed"]
    assert "markers remain" in captured[0].payload["reason"]
