"""Core agentic conversation loop.

Handles the multi-turn cycle: prompt -> LLM response -> tool execution -> continue.
Supports pass-through mode for CLI providers that manage their own tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from agent.context import ContextManager
from agent.context.reactive_compact import PromptTooLongError
from agent.llm.types import LLMResponse, Message, TokenUsage, ToolCall
from agent.session import Session
from agent.tools.base import ToolContext, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from agent.llm.base import LLMProvider

logger = structlog.get_logger()


@dataclass
class AgentResult:
    """Final result from an agent run."""

    output: str
    tool_calls_made: int = 0
    tokens_used: TokenUsage = field(default_factory=TokenUsage)
    messages: list[Message] = field(default_factory=list)
    api_messages: list[Message] = field(default_factory=list)


class AgentLoop:
    """The core agentic conversation loop.

    For API providers (Anthropic, OpenAI): runs the full tool-calling loop
    with context management and session persistence.

    For pass-through providers (Claude CLI): sends the prompt as a subprocess
    and returns the raw output. No tool execution or context management.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry,
        context_manager: ContextManager,
        session: Session | None = None,
        max_turns: int = 50,
        workspace: str = ".",
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._context = context_manager
        self._session = session
        self._max_turns = max_turns
        self._workspace = workspace

    async def run(
        self,
        prompt: str,
        system: str | None = None,
        resume: bool = False,
    ) -> AgentResult:
        """Run the agent loop until completion or max_turns.

        Args:
            prompt: User prompt / task instructions.
            system: Override system prompt (if None, builds from workspace context).
            resume: If True and session exists, continue from saved state.
        """
        # Pass-through mode for CLI providers
        if self._provider.is_passthrough:
            return await self._run_passthrough(prompt, resume)

        return await self._run_agentic(prompt, system, resume)

    # ------------------------------------------------------------------
    # Pass-through mode (Claude Code CLI)
    # ------------------------------------------------------------------

    async def _run_passthrough(self, prompt: str, resume: bool) -> AgentResult:
        """Send prompt to CLI provider, return raw output."""
        from agent.llm.claude_cli import ClaudeCLIProvider

        if isinstance(self._provider, ClaudeCLIProvider) and self._session:
            self._provider.set_session(self._session.session_id, resume=resume)

        response = await self._provider.complete(
            messages=[Message(role="user", content=prompt)],
        )
        return AgentResult(
            output=response.message.content,
            tokens_used=response.usage,
        )

    # ------------------------------------------------------------------
    # Full agentic loop (API providers)
    # ------------------------------------------------------------------

    async def _run_agentic(
        self,
        prompt: str,
        system: str | None,
        resume: bool,
    ) -> AgentResult:
        # Build system prompt if not provided
        if system is None:
            system = await self._context.build_system_prompt()

        # Load or initialize conversation
        messages: list[Message] = []
        api_messages: list[Message] = []

        if resume and self._session:
            loaded = await self._session.load()
            if loaded:
                messages, api_messages = loaded

        # Append user prompt
        user_msg = Message(role="user", content=prompt)
        messages.append(user_msg)
        api_messages.append(user_msg)

        tool_context = ToolContext(workspace=self._workspace, readonly=False)
        total_tool_calls = 0
        cumulative_usage = TokenUsage()
        tool_defs = self._tools.definitions()

        for turn in range(self._max_turns):
            # Run context compaction pipeline
            api_messages = await self._context.prepare(api_messages, system, tool_defs)

            # Call the LLM
            try:
                response = await self._provider.complete(
                    messages=api_messages,
                    tools=tool_defs if tool_defs else None,
                    system=system,
                )
            except Exception as e:
                error_str = str(e)
                # Detect prompt-too-long errors
                if "prompt_too_long" in error_str.lower() or "413" in error_str:
                    logger.warning("prompt_too_long_detected", turn=turn)
                    try:
                        api_messages = await self._context.handle_prompt_too_long(api_messages)
                        continue  # Retry with compacted messages
                    except PromptTooLongError:
                        return AgentResult(
                            output="[ERROR] Conversation too large — all compaction attempts failed.",
                            messages=messages,
                            api_messages=api_messages,
                            tokens_used=cumulative_usage,
                        )
                else:
                    logger.error("llm_call_failed", error=error_str, turn=turn)
                    return AgentResult(
                        output=f"[ERROR] LLM call failed: {error_str}",
                        messages=messages,
                        api_messages=api_messages,
                        tokens_used=cumulative_usage,
                    )

            # Accumulate usage
            cumulative_usage.input_tokens += response.usage.input_tokens
            cumulative_usage.output_tokens += response.usage.output_tokens

            # Add assistant message to both histories
            messages.append(response.message)
            api_messages.append(response.message)

            # Check stop reason
            if response.stop_reason == "end_turn":
                break

            if response.stop_reason == "max_tokens":
                # Inject continuation message
                continuation = Message(
                    role="user",
                    content="Output was truncated. Continue exactly where you left off.",
                )
                messages.append(continuation)
                api_messages.append(continuation)
                continue

            if response.stop_reason == "tool_use" and response.message.tool_calls:
                # Execute each tool call
                for tc in response.message.tool_calls:
                    result = await self._execute_tool(tc, tool_context)
                    total_tool_calls += 1

                    tool_msg = Message(
                        role="tool",
                        content=result.output,
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        token_estimate=result.token_estimate,
                    )
                    messages.append(tool_msg)
                    api_messages.append(tool_msg)

                    logger.debug(
                        "tool_executed",
                        tool=tc.name,
                        is_error=result.is_error,
                        turn=turn,
                    )

        else:
            # Max turns reached
            logger.warning("max_turns_reached", max_turns=self._max_turns)

        # Save session
        if self._session:
            await self._session.save(messages, api_messages)

        # Extract final output (last assistant message text)
        output = ""
        for msg in reversed(messages):
            if msg.role == "assistant" and msg.content:
                output = msg.content
                break

        return AgentResult(
            output=output,
            tool_calls_made=total_tool_calls,
            tokens_used=cumulative_usage,
            messages=messages,
            api_messages=api_messages,
        )

    async def _execute_tool(self, tool_call: ToolCall, context: ToolContext) -> ToolResult:
        """Execute a single tool call, handling missing tools gracefully."""
        tool = self._tools.get(tool_call.name)
        if not tool:
            return ToolResult(
                output=f"Error: unknown tool '{tool_call.name}'. Available: {', '.join(self._tools.names())}",
                is_error=True,
            )

        try:
            return await tool.execute(tool_call.arguments, context)
        except Exception as e:
            logger.error("tool_execution_error", tool=tool_call.name, error=str(e))
            return ToolResult(
                output=f"Error executing {tool_call.name}: {e}",
                is_error=True,
            )
