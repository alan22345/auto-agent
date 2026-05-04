"""Tests for agent/sh.py — exercised against real subprocesses.

We deliberately do NOT mock ``asyncio.create_subprocess_*`` here. The
seam's whole job is owning the timeout-with-kill / decode / env-merge
invariants — testing it through real ``python3 -c`` invocations is the
only way to catch a regression where, say, the kill path is forgotten.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from agent.sh import RunResult, run, run_shell


@pytest.mark.asyncio
async def test_run_captures_stdout_stderr_and_returncode():
    result = await run(
        [sys.executable, "-c", "import sys; sys.stdout.write('out'); sys.stderr.write('err')"],
        timeout=5,
    )
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.failed is False


@pytest.mark.asyncio
async def test_run_returns_nonzero_returncode():
    result = await run(
        [sys.executable, "-c", "import sys; sys.exit(7)"],
        timeout=5,
    )
    assert result.returncode == 7
    assert result.failed is True


@pytest.mark.asyncio
async def test_run_kills_on_timeout():
    """A process that sleeps past the deadline must be killed, not leaked."""
    start = time.monotonic()
    result = await run(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        timeout=0.5,
    )
    elapsed = time.monotonic() - start
    assert result.timed_out is True
    assert result.failed is True
    # Wall-clock proves the kill path ran rather than us waiting 10s.
    assert elapsed < 5, f"timeout-with-kill took {elapsed:.2f}s — kill likely didn't fire"


@pytest.mark.asyncio
async def test_run_default_sets_git_terminal_prompt_zero():
    result = await run(
        [sys.executable, "-c", "import os; print(os.environ.get('GIT_TERMINAL_PROMPT', 'unset'))"],
        timeout=5,
    )
    assert result.stdout.strip() == "0"


@pytest.mark.asyncio
async def test_run_caller_env_overrides_default():
    result = await run(
        [sys.executable, "-c", "import os; print(os.environ.get('GIT_TERMINAL_PROMPT'))"],
        timeout=5,
        env={"GIT_TERMINAL_PROMPT": "1"},
    )
    assert result.stdout.strip() == "1"


@pytest.mark.asyncio
async def test_run_extra_env_is_merged_with_os_environ():
    # OS env should still be visible alongside the caller's extras.
    os.environ["AGENT_SH_TEST_BASE"] = "from_os"
    try:
        result = await run(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('AGENT_SH_TEST_BASE'), os.environ.get('AGENT_SH_TEST_EXTRA'))",
            ],
            timeout=5,
            env={"AGENT_SH_TEST_EXTRA": "from_caller"},
        )
    finally:
        os.environ.pop("AGENT_SH_TEST_BASE", None)
    assert result.stdout.strip() == "from_os from_caller"


@pytest.mark.asyncio
async def test_run_decode_replaces_invalid_bytes():
    result = await run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe ok')"],
        timeout=5,
    )
    assert "ok" in result.stdout
    # No UnicodeDecodeError surfaced.
    assert isinstance(result.stdout, str)


@pytest.mark.asyncio
async def test_run_shell_executes_through_shell():
    result = await run_shell("echo $((1+2))", timeout=5)
    assert result.stdout.strip() == "3"
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_run_max_output_truncates_with_label():
    result = await run(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200_000)"],
        timeout=10,
        max_output=1000,
    )
    assert len(result.stdout) < 200_000
    assert "(truncated," in result.stdout


@pytest.mark.asyncio
async def test_run_stderr_to_stdout_merges_streams():
    result = await run(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('A'); sys.stderr.write('B')",
        ],
        timeout=5,
        stderr_to_stdout=True,
    )
    assert "A" in result.stdout
    assert "B" in result.stdout
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_run_argv_recorded_in_result():
    result = await run([sys.executable, "-c", "pass"], timeout=5)
    assert result.argv == (sys.executable, "-c", "pass")


@pytest.mark.asyncio
async def test_run_shell_argv_records_command_string():
    result = await run_shell("true", timeout=5)
    assert result.argv == ("true",)


def test_run_result_failed_predicate():
    assert RunResult("", "", 0, False, ()).failed is False
    assert RunResult("", "", 1, False, ()).failed is True
    assert RunResult("", "", None, True, ()).failed is True
