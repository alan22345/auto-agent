"""Claude Code CLI provider — wraps the existing subprocess for A/B comparison.

When this provider is selected, the AgentLoop runs in pass-through mode:
no tool execution, no context management. The CLI handles everything internally.
This gives a clean baseline to compare against direct API providers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import uuid

from agent.llm.base import LLMProvider
from agent.llm.types import (
    LLMResponse,
    Message,
    TokenUsage,
    ToolDefinition,
)

log = logging.getLogger(__name__)

# Substring the Claude CLI emits on stderr when a session ID is already
# registered (stale lock from a crashed prior invocation). The provider
# detects this and rotates to a fresh UUID so the caller doesn't get a
# CLI-error string back as if it were an LLM response.
_SESSION_ALREADY_IN_USE = "already in use"


def _killpg(pid: int | None) -> None:
    """SIGKILL the process group led by ``pid``. Safe if the group is
    already gone (no leader, no descendants) — ``ProcessLookupError`` is
    swallowed. Used to nuke detached children (smoke-agent dev servers)
    that claude spawned and didn't reap.
    """
    if pid is None:
        return
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


class ClaudeCLIProvider(LLMProvider):
    """Pass-through provider that delegates to the Claude Code CLI binary."""

    model = "claude-cli"
    max_context_tokens = 200_000
    is_passthrough = True

    def __init__(self, timeout: int = 3600, home_dir: str | None = None):
        self._timeout = timeout
        self._session_id: str | None = None
        self._cwd: str | None = None
        self._home_dir: str | None = home_dir
        self._resume: bool = False

    def set_cwd(self, cwd: str) -> None:
        """Set the working directory for CLI invocations."""
        self._cwd = cwd

    def set_home_dir(self, home_dir: str) -> None:
        """Set HOME for CLI invocations — selects the user's credential vault."""
        self._home_dir = home_dir

    def set_session(self, session_id: str, resume: bool = False) -> None:
        """Configure session for multi-phase tasks.

        Claude CLI requires ``--session-id <uuid>`` to be a valid UUID and
        rejects anything else instantly — which the rotate-recovery doesn't
        catch (it only triggers on "already in use"). A non-UUID caller
        leaks ``[ERROR] CLI exited N`` back as the model response and trips
        downstream emptiness checks (task 28, 2026-05-27). Coerce non-UUID
        IDs to a deterministic UUID5 so the (caller, label) mapping stays
        stable and ``--resume`` still works for the same logical session.
        """
        try:
            uuid.UUID(session_id)
            coerced = session_id
        except (TypeError, ValueError, AttributeError):
            coerced = str(uuid.uuid5(uuid.NAMESPACE_URL, f"claude-cli-{session_id}"))
            log.warning(
                "ClaudeCLIProvider received non-UUID session_id %r; coerced to %s",
                session_id,
                coerced,
            )
        self._session_id = coerced
        self._resume = resume

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        # Extract the latest user message as the prompt
        prompt = ""
        for msg in reversed(messages):
            if msg.role == "user":
                prompt = msg.content
                break

        if not prompt:
            return LLMResponse(
                message=Message(role="assistant", content="[No prompt provided]"),
                stop_reason="error",
            )

        import json as _json

        raw = await self._run_cli(prompt)
        text = raw
        usage = TokenUsage()
        try:
            envelope = _json.loads(raw)
            if isinstance(envelope, dict):
                # Final-result envelope shape:
                # {"type":"result","result":"...","usage":{"input_tokens":N,"output_tokens":M, ...}}
                result_text = envelope.get("result")
                if isinstance(result_text, str):
                    text = result_text
                u = envelope.get("usage")
                if isinstance(u, dict):
                    in_tok = int(u.get("input_tokens") or 0)
                    out_tok = int(u.get("output_tokens") or 0)
                    # Cache reads are billed at a fraction; we surface them as input
                    # tokens for now (UsageSink doesn't distinguish yet).
                    cache_read = int(u.get("cache_read_input_tokens") or 0)
                    usage = TokenUsage(
                        input_tokens=in_tok + cache_read,
                        output_tokens=out_tok,
                    )
        except (ValueError, TypeError):
            # Non-JSON output (older Claude Code, or an error path). Surface the
            # raw text and leave usage at zero — emit_usage_event will record a
            # zero-token event rather than crash.
            pass

        return LLMResponse(
            message=Message(role="assistant", content=text),
            stop_reason="end_turn",
            usage=usage,
        )

    async def count_tokens(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
    ) -> int:
        # CLI manages its own context — return 0 so compaction never triggers
        return 0

    async def _invoke_cli_once(self, prompt: str) -> tuple[str, str, int | None]:
        """Run the Claude Code CLI as a subprocess once. Returns (stdout, stderr, returncode).

        Returncode is None on timeout. Caller decides whether to retry.

        The prompt is piped via stdin (not argv). Large grill summaries (>~100KB)
        used to blow past the kernel's ``ARG_MAX`` and produce
        ``OSError: [Errno 7] Argument list too long`` when ``claude`` was
        spawned with the prompt as a trailing positional. ``claude --print``
        reads its prompt from stdin when no positional is provided, so the
        stdin path has no practical ceiling (Linux pipe buffer is 64KB but
        ``communicate(input=...)`` drains it as the CLI reads).
        """
        cmd = ["claude", "--print", "--output-format", "json", "--dangerously-skip-permissions"]

        if self._session_id:
            if self._resume:
                cmd.extend(["--resume", self._session_id])
            else:
                cmd.extend(["--session-id", self._session_id])

        # ``start_new_session=True`` makes the claude child a process-group
        # leader. Any descendants it spawns (smoke-agent dev servers — vite,
        # uvicorn, npm, esbuild, the bash wrappers around them) inherit the
        # group. On exit/timeout we ``killpg(SIGKILL)`` the whole group so a
        # detached dev server can't keep ``proc.communicate()`` blocked
        # forever. Task 28 (2026-05-27) wedged twice in one session because
        # the smoke agent's bash kept the parent's stdio fds alive after
        # claude itself was done. (handover: smoke-agent wedges claude)
        kwargs: dict = dict(
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if self._home_dir is not None:
            kwargs["env"] = {**os.environ, "HOME": self._home_dir}

        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()), timeout=self._timeout
            )
            return (
                (stdout or b"").decode(),
                (stderr or b"").decode(),
                proc.returncode,
            )
        except TimeoutError:
            _killpg(getattr(proc, "pid", None))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.communicate(), timeout=2.0)
            return ("", "", None)
        finally:
            # Even on the happy path, claude may have spawned a detached
            # process (smoke-agent dev server) that's still holding the pgid.
            # Nuke the whole group; the claude leader has already exited so
            # this only reaps stragglers.
            _killpg(getattr(proc, "pid", None))

    async def _run_cli(self, prompt: str) -> str:
        """Run the Claude Code CLI, recovering from session-already-in-use collisions.

        If the CLI rejects a deterministic session ID as already-in-use
        (stale lock from a crashed prior invocation, or a caller passing the
        same hash twice), rotate ``self._session_id`` to a fresh UUID and
        retry once. Without this recovery, the error string leaks back as
        if it were an LLM response — and the calling code (e.g. the
        independent reviewer) treats it as review feedback. Skip the
        rotation for resume mode: --resume EXPECTS the session to exist.
        """
        output, errors, returncode = await self._invoke_cli_once(prompt)
        if returncode is None:
            return "[ERROR] Claude Code CLI timed out"

        if (
            returncode != 0
            and not self._resume
            and self._session_id
            and _SESSION_ALREADY_IN_USE in errors
        ):
            stale = self._session_id
            self._session_id = str(uuid.uuid4())
            log.warning(
                "Claude CLI session %s already in use; rotated to %s and retrying",
                stale,
                self._session_id,
            )
            output, errors, returncode = await self._invoke_cli_once(prompt)
            if returncode is None:
                return "[ERROR] Claude Code CLI timed out"

        if returncode != 0 and not output.strip():
            return f"[ERROR] CLI exited {returncode}: {errors.strip()}"
        return output
