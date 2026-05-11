"""Claude Code CLI provider — wraps the existing subprocess for A/B comparison.

When this provider is selected, the AgentLoop runs in pass-through mode:
no tool execution, no context management. The CLI handles everything internally.
This gives a clean baseline to compare against direct API providers.
"""

from __future__ import annotations

import asyncio
import logging
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

    def set_cwd(self, cwd: str) -> None:
        """Set the working directory for CLI invocations."""
        self._cwd = cwd

    def set_home_dir(self, home_dir: str) -> None:
        """Set HOME for CLI invocations — selects the user's credential vault."""
        self._home_dir = home_dir

    def set_session(self, session_id: str, resume: bool = False) -> None:
        """Configure session for multi-phase tasks."""
        self._session_id = session_id
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

        output = await self._run_cli(prompt)
        return LLMResponse(
            message=Message(role="assistant", content=output),
            stop_reason="end_turn",
            usage=TokenUsage(),  # CLI doesn't report token usage
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
        """
        cmd = ["claude", "--print", "--dangerously-skip-permissions"]

        if self._session_id:
            if self._resume:
                cmd.extend(["--resume", self._session_id])
            else:
                cmd.extend(["--session-id", self._session_id])

        cmd.append(prompt)

        kwargs: dict = dict(
            cwd=self._cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if self._home_dir is not None:
            import os

            kwargs["env"] = {**os.environ, "HOME": self._home_dir}

        proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return ("", "", None)

        return (
            (stdout or b"").decode(),
            (stderr or b"").decode(),
            proc.returncode,
        )

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
                stale, self._session_id,
            )
            output, errors, returncode = await self._invoke_cli_once(prompt)
            if returncode is None:
                return "[ERROR] Claude Code CLI timed out"

        if returncode != 0 and not output.strip():
            return f"[ERROR] CLI exited {returncode}: {errors.strip()}"
        return output
