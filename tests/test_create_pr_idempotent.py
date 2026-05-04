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

Tests inject a fake :class:`agent.sh.RunResult` instead of mocking the raw
``asyncio.create_subprocess_exec`` — the seam (``agent/sh.py``) owns
the subprocess invariant and is exercised against real processes in
``tests/test_sh.py``.
"""

from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle.review import find_existing_pr_url
from agent.sh import RunResult


def _result(stdout: str = "", *, returncode: int = 0, timed_out: bool = False) -> RunResult:
    return RunResult(
        stdout=stdout,
        stderr="",
        returncode=None if timed_out else returncode,
        timed_out=timed_out,
    )


@pytest.mark.asyncio
async def test_returns_existing_pr_url_if_present(tmp_path):
    """gh pr list returns a JSON record → we extract the URL."""
    fake = _result('[{"url":"https://github.com/org/repo/pull/42","number":42,"state":"OPEN"}]')
    with patch("agent.lifecycle.review.sh.run", new=AsyncMock(return_value=fake)):
        url = await find_existing_pr_url(str(tmp_path), "feature/x")
    assert url == "https://github.com/org/repo/pull/42"


@pytest.mark.asyncio
async def test_returns_none_if_no_pr(tmp_path):
    """gh pr list returns [] → None."""
    fake = _result("[]")
    with patch("agent.lifecycle.review.sh.run", new=AsyncMock(return_value=fake)):
        url = await find_existing_pr_url(str(tmp_path), "feature/y")
    assert url is None


@pytest.mark.asyncio
async def test_returns_none_on_gh_error(tmp_path):
    """If gh fails, fall through to None (let create_pr handle it)."""
    fake = _result("", returncode=1)
    with patch("agent.lifecycle.review.sh.run", new=AsyncMock(return_value=fake)):
        url = await find_existing_pr_url(str(tmp_path), "feature/z")
    assert url is None


@pytest.mark.asyncio
async def test_returns_first_open_pr_if_multiple(tmp_path):
    """Multiple PRs on the branch (unusual but possible) → use the first open one."""
    fake = _result(
        '[{"url":"https://github.com/a/b/pull/1","state":"OPEN"},'
        '{"url":"https://github.com/a/b/pull/2","state":"OPEN"}]'
    )
    with patch("agent.lifecycle.review.sh.run", new=AsyncMock(return_value=fake)):
        url = await find_existing_pr_url(str(tmp_path), "feature/x")
    assert url == "https://github.com/a/b/pull/1"


@pytest.mark.asyncio
async def test_returns_none_on_timeout(tmp_path):
    """Network stall → gh pr list times out, returns None instead of hanging.

    Regression for the no-timeout bug fixed by ADR-010: ``find_existing_pr_url``
    used to call ``create_subprocess_exec`` without ``wait_for``, so a stalled
    GitHub API would block the agent loop indefinitely. Routing through
    ``agent/sh.py`` bounds it at 20s and surfaces ``timed_out=True``, which we
    treat as "no existing PR found, fall through to gh pr create".
    """
    fake = _result(timed_out=True)
    with patch("agent.lifecycle.review.sh.run", new=AsyncMock(return_value=fake)):
        url = await find_existing_pr_url(str(tmp_path), "feature/x")
    assert url is None
