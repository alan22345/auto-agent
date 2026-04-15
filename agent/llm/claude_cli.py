"""Claude Code CLI provider — wraps the existing subprocess for A/B comparison.

When this provider is selected, the AgentLoop runs in pass-through mode:
no tool execution, no context management. The CLI handles everything internally.
This gives a clean baseline to compare against direct API providers.
"""

from __future__ import annotations

import asyncio
import uuid

from agent.llm.base import LLMProvider
from agent.llm.types import (
    LLMResponse,
    Message,
    TokenUsage,
    ToolDefinition,
)


class ClaudeCLIProvider(LLMProvider):
    """Pass-through provider that delegates to the Claude Code CLI binary."""

    model = "claude-cli"
    max_context_tokens = 200_000
    is_passthrough = True

    def __init__(self, timeout: int = 1200):
        self._timeout = timeout
        self._session_id: str | None = None

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

    async def _run_cli(self, prompt: str) -> str:
        """Run the Claude Code CLI as a subprocess."""
        cmd = ["claude", "--print", "--dangerously-skip-permissions"]

        if self._session_id:
            if self._resume:
                cmd.extend(["--resume", self._session_id])
            else:
                cmd.extend(["--session-id", self._session_id])

        cmd.append(prompt)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return "[ERROR] Claude Code CLI timed out"

        output = (stdout or b"").decode()
        errors = (stderr or b"").decode()
        if proc.returncode != 0 and not output.strip():
            return f"[ERROR] CLI exited {proc.returncode}: {errors.strip()}"
        return output
