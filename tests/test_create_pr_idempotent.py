"""Regression test: create_pr should be idempotent.

Task 51 scenario:
  - Coding finished, PR #115 created, reached AWAITING_REVIEW.
  - Deploy failed → task transitioned back to CODING to fix the issue.
  - After the fix, _finish_coding ran again and called `gh pr create` on
    the same branch → failed with "pull request for branch X already
    exists" → task FAILED.

Expected behavior: if a PR already exists for the head branch, return its
URL instead of trying to create a new one. The new commits pushed to the
branch are automatically reflected in the existing PR.
"""

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from agent.lifecycle.review import find_existing_pr_url


@pytest.mark.asyncio
async def test_returns_existing_pr_url_if_present(tmp_path):
    """gh pr list returns a JSON record → we extract the URL."""
    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(
            b'[{"url":"https://github.com/org/repo/pull/42","number":42,"state":"OPEN"}]',
            b"",
        ))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        url = await find_existing_pr_url(str(tmp_path), "feature/x")
    assert url == "https://github.com/org/repo/pull/42"


@pytest.mark.asyncio
async def test_returns_none_if_no_pr(tmp_path):
    """gh pr list returns [] → None."""
    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"[]", b""))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        url = await find_existing_pr_url(str(tmp_path), "feature/y")
    assert url is None


@pytest.mark.asyncio
async def test_returns_none_on_gh_error(tmp_path):
    """If gh fails, fall through to None (let create_pr handle it)."""
    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", b"auth error"))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        url = await find_existing_pr_url(str(tmp_path), "feature/z")
    assert url is None


@pytest.mark.asyncio
async def test_returns_first_open_pr_if_multiple(tmp_path):
    """Multiple PRs on the branch (unusual but possible) → use the first open one."""
    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(
            b'[{"url":"https://github.com/a/b/pull/1","state":"OPEN"},'
            b'{"url":"https://github.com/a/b/pull/2","state":"OPEN"}]',
            b"",
        ))
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        url = await find_existing_pr_url(str(tmp_path), "feature/x")
    assert url == "https://github.com/a/b/pull/1"
