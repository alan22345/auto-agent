"""Integration test: BashTool/GitTool compose ToolResult from a fake RunResult.

The seam (``agent/sh.py``) is exercised against real subprocesses in
``tests/test_sh.py``. These tests pin the LLM-facing-string formatting
that each tool wraps around a ``RunResult`` — without spinning up a
subprocess at all.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.sh import RunResult
from agent.tools.base import ToolContext
from agent.tools.bash import BashTool
from agent.tools.git import GitTool


@pytest.mark.asyncio
async def test_bash_formats_stdout_and_stderr(tmp_path):
    fake = RunResult(stdout="hello\n", stderr="warn\n", returncode=0, timed_out=False)
    with patch("agent.tools.bash.sh.run_shell", new=AsyncMock(return_value=fake)):
        result = await BashTool().execute({"command": "ls"}, ToolContext(workspace=str(tmp_path)))
    assert "hello" in result.output
    assert "STDERR:\nwarn" in result.output
    assert result.is_error is False


@pytest.mark.asyncio
async def test_bash_formats_non_zero_exit_code(tmp_path):
    fake = RunResult(stdout="", stderr="boom", returncode=2, timed_out=False)
    with patch("agent.tools.bash.sh.run_shell", new=AsyncMock(return_value=fake)):
        result = await BashTool().execute({"command": "ls"}, ToolContext(workspace=str(tmp_path)))
    assert "Exit code: 2" in result.output
    assert result.is_error is True


@pytest.mark.asyncio
async def test_bash_reports_timeout(tmp_path):
    fake = RunResult(stdout="", stderr="", returncode=None, timed_out=True)
    with patch("agent.tools.bash.sh.run_shell", new=AsyncMock(return_value=fake)):
        result = await BashTool().execute(
            {"command": "sleep 99", "timeout": 1}, ToolContext(workspace=str(tmp_path))
        )
    assert "Command timed out after 1s: sleep 99" in result.output
    assert result.is_error is True


@pytest.mark.asyncio
async def test_git_only_shows_stderr_on_failure(tmp_path):
    # Success: stderr (e.g. progress noise) is suppressed.
    ok = RunResult(
        stdout="On branch main\n",
        stderr="warning: ...\n",
        returncode=0,
        timed_out=False,
    )
    with patch("agent.tools.git.sh.run", new=AsyncMock(return_value=ok)):
        result = await GitTool().execute(
            {"command": "status"}, ToolContext(workspace=str(tmp_path))
        )
    assert "On branch main" in result.output
    assert "STDERR" not in result.output

    # Failure: stderr is included.
    fail = RunResult(
        stdout="",
        stderr="fatal: not a git repo",
        returncode=128,
        timed_out=False,
    )
    with patch("agent.tools.git.sh.run", new=AsyncMock(return_value=fail)):
        result = await GitTool().execute(
            {"command": "status"}, ToolContext(workspace=str(tmp_path))
        )
    assert "STDERR: fatal: not a git repo" in result.output
    assert result.is_error is True


@pytest.mark.asyncio
async def test_git_reports_timeout(tmp_path):
    fake = RunResult(stdout="", stderr="", returncode=None, timed_out=True)
    with patch("agent.tools.git.sh.run", new=AsyncMock(return_value=fake)):
        result = await GitTool().execute(
            {"command": "status"}, ToolContext(workspace=str(tmp_path))
        )
    assert "git command timed out" in result.output
    assert result.is_error is True


@pytest.mark.asyncio
async def test_git_blocks_destructive(tmp_path):
    """Policy still enforced — sh.run is never called."""
    with patch("agent.tools.git.sh.run", new=AsyncMock()) as run_mock:
        result = await GitTool().execute(
            {"command": "reset --hard"}, ToolContext(workspace=str(tmp_path))
        )
    assert result.is_error is True
    assert "blocked" in result.output
    run_mock.assert_not_called()
