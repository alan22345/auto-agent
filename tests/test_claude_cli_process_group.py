"""Pin the contract that ClaudeCLIProvider isolates claude in its own
process group and SIGKILLs the group on exit.

Regression context: on task 28 (2026-05-27), the smoke-agent — running
under ``claude --print`` passthrough — spawned a dev server (uvicorn/
vite/npm/esbuild) via its own Bash tool to verify a UI change. The dev
server inherited claude's stdio fds and ran forever; claude itself
exited but the bash wrapper holding the pipe never did, so
``proc.communicate()`` in ``_invoke_cli_once`` blocked indefinitely.
Each wedge required manual SIGKILL of the dev server inside the
container to unblock the auto-agent run.

The fix: spawn claude with ``start_new_session=True`` (so it leads a
new process group) and ``killpg(SIGKILL)`` the whole group after
communicate returns or on timeout. The claude leader has already
exited on the happy path; the killpg only reaps detached stragglers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.llm.claude_cli import ClaudeCLIProvider


@pytest.mark.asyncio
async def test_subprocess_starts_new_session():
    """``start_new_session=True`` must be in the spawn kwargs.

    Without it, claude inherits the auto-agent's process group and any
    detached child it spawns (smoke-agent dev server) keeps the parent
    alive by holding the stdio fds.
    """
    provider = ClaudeCLIProvider()
    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.communicate = AsyncMock(return_value=(b"hello", b""))
    fake_proc.returncode = 0

    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)) as spawn,
        patch("agent.llm.claude_cli._killpg") as killpg,
    ):
        await provider._invoke_cli_once("hi")

    assert spawn.call_args.kwargs.get("start_new_session") is True
    # Process-group kill MUST run on the happy path too — detached
    # children outlive their claude parent.
    killpg.assert_called_with(12345)


@pytest.mark.asyncio
async def test_killpg_runs_on_timeout():
    """On timeout, the process group is SIGKILLed before returning."""
    provider = ClaudeCLIProvider(timeout=1)
    fake_proc = MagicMock()
    fake_proc.pid = 22222
    # First communicate blocks past the deadline; second (after kill) returns.
    fake_proc.communicate = AsyncMock(side_effect=[TimeoutError(), (b"", b"")])
    fake_proc.returncode = None

    async def fake_wait_for(coro, timeout):
        # Drain the coroutine so AsyncMock advances side_effect.
        try:
            return await coro
        except TimeoutError:
            raise

    with (
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)),
        patch("asyncio.wait_for", side_effect=fake_wait_for),
        patch("agent.llm.claude_cli._killpg") as killpg,
    ):
        _out, _err, rc = await provider._invoke_cli_once("hi")

    assert rc is None  # timeout sentinel
    # killpg called at least once (once in the except, once in finally).
    assert killpg.called
    assert killpg.call_args_list[0].args[0] == 22222


def test_killpg_swallows_missing_process():
    """``_killpg`` must not raise when the pid is already gone."""
    from agent.llm.claude_cli import _killpg

    # PID 1 belongs to init; we lack permission to signal its group,
    # and the function should swallow PermissionError silently.
    _killpg(1)  # no exception

    # A pid that almost certainly doesn't exist.
    _killpg(2_000_000)  # no exception
