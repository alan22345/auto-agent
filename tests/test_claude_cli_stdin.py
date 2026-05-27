"""Pin the contract that ClaudeCLIProvider pipes the prompt via stdin, not argv.

Regression context: when the scaffold flow's domain architect produced a
112KB grill summary that landed in the next CLI turn's prompt, the
subprocess spawn used to crash with::

    OSError: [Errno 7] Argument list too long
    File "agent/llm/claude_cli.py", in _invoke_cli_once
        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)

— the Linux ``ARG_MAX`` kernel limit is ~128KB (sum of argv + env).
Domains 001–005 had 30–83KB grills and worked; 006 at 112KB tipped over.
On uvloop the same condition manifested as a silent forever-hang at first
(no log, no error) and then surfaced as the OSError after a container
restart pushed the spawn into the error path.

The fix: pipe the prompt via stdin (the Claude CLI reads from stdin when
no positional prompt is given). No ARG_MAX ceiling.

If a future edit reintroduces the argv path, this test fails.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.llm.claude_cli import ClaudeCLIProvider


@pytest.mark.asyncio
async def test_prompt_is_not_in_argv():
    """Spawned argv must not contain the prompt body."""
    provider = ClaudeCLIProvider()
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"hello", b""))
    fake_proc.returncode = 0
    spawn = AsyncMock(return_value=fake_proc)

    huge_prompt = "X" * 200_000  # 200KB — well past ARG_MAX
    with patch("asyncio.create_subprocess_exec", spawn):
        out, err, rc = await provider._invoke_cli_once(huge_prompt)

    assert rc == 0
    assert out == "hello"
    # The spawn args (the *cmd tuple) must not contain the prompt body.
    cmd = spawn.call_args.args
    assert huge_prompt not in cmd, "prompt must not be passed via argv"
    assert all("X" * 100 not in part for part in cmd), (
        "no argv element should contain the prompt content (ARG_MAX regression)"
    )


@pytest.mark.asyncio
async def test_prompt_is_sent_via_stdin():
    """``communicate(input=prompt.encode())`` is how the prompt reaches the CLI."""
    provider = ClaudeCLIProvider()
    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"hello", b""))
    fake_proc.returncode = 0

    with patch(
        "asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)
    ) as spawn:
        await provider._invoke_cli_once("summarise this please")

    # stdin pipe was opened
    kwargs = spawn.call_args.kwargs
    assert kwargs.get("stdin") == asyncio.subprocess.PIPE, (
        "stdin must be opened so we can pipe the prompt"
    )
    # The prompt was written to stdin via communicate(input=...)
    fake_proc.communicate.assert_awaited_once()
    input_bytes = fake_proc.communicate.call_args.kwargs.get("input")
    assert input_bytes == b"summarise this please"
