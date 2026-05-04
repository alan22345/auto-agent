"""Subprocess execution seam.

Owns the async-subprocess invariant for the agent layer:

  - argv-vs-shell selection (``run`` for argv, ``run_shell`` for a shell line)
  - env merge: ``os.environ`` then ``{"GIT_TERMINAL_PROMPT": "0"}`` then
    caller env (caller wins on conflict)
  - timeout-with-kill: on timeout, ``proc.kill()`` + drain via ``communicate()``,
    surfaced as ``RunResult(timed_out=True)``
  - decode-with-replace on stdout/stderr
  - optional ``max_output`` truncation with a labelled tail
  - optional ``stderr_to_stdout`` stream redirection

Every async-subprocess call site in the agent layer routes through this
module. Tools and lifecycle handlers compose their LLM-facing strings
from the returned ``RunResult``; this module owns no domain knowledge.

See docs/decisions/010-subprocess-seam.md for the deletion-test
justification and the deliberate scope decision (``llm/claude_cli.py``
sits behind the ``LLMProvider`` seam and stays out).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class RunResult:
    """Outcome of one subprocess invocation.

    ``returncode`` is ``None`` only when ``timed_out`` is ``True`` and the
    process was killed before exiting. ``failed`` is the canonical
    "did this go wrong" predicate that callers should branch on.
    """

    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool
    argv: tuple[str, ...]

    @property
    def failed(self) -> bool:
        return self.timed_out or (self.returncode is not None and self.returncode != 0)


def _build_env(extra: Mapping[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    if extra:
        env.update(extra)
    return env


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (truncated, {len(text)} total chars)"


async def _drain(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """After ``proc.kill()``, drain pending pipe output without blocking.

    Returns ``(b"", b"")`` if the drain itself raises — we never want the
    cleanup path to surface a new exception that masks the timeout.
    """
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=2)
    except Exception:
        return (b"", b"")


async def run(
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout: float,
    env: Mapping[str, str] | None = None,
    stderr_to_stdout: bool = False,
    max_output: int | None = None,
) -> RunResult:
    """Run ``argv`` via ``create_subprocess_exec`` with a hard timeout.

    On ``asyncio.TimeoutError`` the process is killed and drained; the
    returned ``RunResult`` carries ``timed_out=True``. ``GIT_TERMINAL_PROMPT=0``
    is set by default so accidentally-prompting git/gh invocations don't
    block forever waiting for a TTY that isn't there.
    """
    stderr_pipe = asyncio.subprocess.STDOUT if stderr_to_stdout else asyncio.subprocess.PIPE
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr_pipe,
        env=_build_env(env),
    )
    return await _await_proc(proc, tuple(argv), timeout, stderr_to_stdout, max_output)


async def run_shell(
    command: str,
    *,
    cwd: str | None = None,
    timeout: float,
    env: Mapping[str, str] | None = None,
    stderr_to_stdout: bool = False,
    max_output: int | None = None,
) -> RunResult:
    """Run ``command`` via ``create_subprocess_shell`` with a hard timeout.

    Use this only for the agent's bash tool and the test_runner — both
    legitimately need shell parsing of a user/agent-supplied string.
    Everything else should use ``run`` with an argv list.
    """
    stderr_pipe = asyncio.subprocess.STDOUT if stderr_to_stdout else asyncio.subprocess.PIPE
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr_pipe,
        env=_build_env(env),
    )
    return await _await_proc(proc, (command,), timeout, stderr_to_stdout, max_output)


async def _await_proc(
    proc: asyncio.subprocess.Process,
    argv: tuple[str, ...],
    timeout: float,
    stderr_to_stdout: bool,
    max_output: int | None,
) -> RunResult:
    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        stdout_b, stderr_b = await _drain(proc)

    stdout = (stdout_b or b"").decode(errors="replace")
    stderr = (stderr_b or b"").decode(errors="replace") if not stderr_to_stdout else ""

    if max_output is not None:
        stdout = _truncate(stdout, max_output)
        if stderr:
            stderr = _truncate(stderr, max_output)

    return RunResult(
        stdout=stdout,
        stderr=stderr,
        returncode=proc.returncode,
        timed_out=timed_out,
        argv=argv,
    )
