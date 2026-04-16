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
from agent.context.workspace_state import WorkspaceState
from agent.llm.types import LLMResponse, Message, TokenUsage, ToolCall
from agent.session import Session
from agent.tools.base import ToolContext, ToolRegistry, ToolResult
from agent.tools.cache import ToolCache

if TYPE_CHECKING:
    from agent.llm.base import LLMProvider

logger = structlog.get_logger()

# Tools that count as "read-only" for exploration budget tracking
_READ_ONLY_TOOLS = frozenset({"file_read", "glob", "grep", "git"})
# Tools that count as "writing" — reset the exploration counter
_WRITE_TOOLS = frozenset({"file_write", "file_edit", "bash"})
# After this many consecutive read-only tool calls, inject a gentle nudge
_EXPLORATION_BUDGET = 8

_EXPLORATION_NUDGE = (
    "You have spent several turns reading files without making any changes. "
    "Start implementing now. Only read more files if you are genuinely blocked."
)

# Verification gate: patterns in bash commands that count as "ran tests/verification"
_VERIFICATION_PATTERNS = frozenset({
    "pytest", "python -m pytest", "npm test", "npx jest", "yarn test",
    "cargo test", "go test", "make test", "ruff check", "ruff format",
    "eslint", "mypy", "tsc", "flake8", "black --check",
})

_VERIFICATION_NUDGE = (
    "You are about to finish without running verification. Before claiming completion:\n"
    "1. Run the test suite (or linter if no tests exist)\n"
    "2. Read the output and confirm it passes\n"
    "3. Only then state your final result with evidence\n"
    "Do NOT say 'should work' or 'looks correct' — show the actual test output."
)


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
        include_methodology: bool = False,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._context = context_manager
        self._session = session
        self._max_turns = max_turns
        self._workspace = workspace
        self._include_methodology = include_methodology

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
            system = await self._context.build_system_prompt(
                include_methodology=self._include_methodology,
            )

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
        consecutive_reads = 0  # Exploration budget tracker
        nudge_injected = False
        has_written = False  # Whether agent has written/edited any files
        has_verified = False  # Whether agent has run tests/linting
        verification_nudge_sent = False
        ws_state = WorkspaceState()  # Track files read/modified/tested
        tool_cache = ToolCache()  # Cache glob/grep results

        for turn in range(self._max_turns):
            ws_state.advance_turn()

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
                # Verification gate: if agent wrote files but never ran tests,
                # nudge it to verify before completing (once only)
                if has_written and not has_verified and not verification_nudge_sent:
                    verification_nudge_sent = True
                    nudge = Message(role="user", content=_VERIFICATION_NUDGE)
                    messages.append(nudge)
                    api_messages.append(nudge)
                    logger.info("verification_nudge_injected", turn=turn)
                    continue  # Give agent another chance to verify
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
                turn_has_write = False
                for tc in response.message.tool_calls:
                    # Check cache for read-only tools
                    cached = tool_cache.get(tc.name, tc.arguments)
                    if cached is not None:
                        result = cached
                        logger.debug("tool_cache_hit", tool=tc.name)
                    else:
                        result = await self._execute_tool(tc, tool_context)
                        tool_cache.put(tc.name, tc.arguments, result)

                    # Invalidate cache on writes
                    tool_cache.invalidate_on_write(tc.name)

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

                    if tc.name in _WRITE_TOOLS:
                        turn_has_write = True

                    # Track writes for verification gate
                    if tc.name in ("file_write", "file_edit"):
                        has_written = True

                    # Track verification commands (test/lint runs via bash or test_runner)
                    if tc.name == "test_runner":
                        has_verified = True
                    elif tc.name == "bash":
                        cmd = tc.arguments.get("command", "")
                        if any(pat in cmd for pat in _VERIFICATION_PATTERNS):
                            has_verified = True

                    # Update workspace state tracker
                    ws_state.process_tool_call(tc.name, tc.arguments)

                # Exploration budget: track consecutive read-only turns
                if turn_has_write:
                    consecutive_reads = 0
                else:
                    tool_names = {tc.name for tc in response.message.tool_calls}
                    if tool_names <= _READ_ONLY_TOOLS:
                        consecutive_reads += 1

                # Inject nudge if budget exceeded (once)
                if consecutive_reads >= _EXPLORATION_BUDGET and not nudge_injected:
                    nudge_msg = Message(role="user", content=_EXPLORATION_NUDGE)
                    messages.append(nudge_msg)
                    api_messages.append(nudge_msg)
                    nudge_injected = True
                    logger.info(
                        "exploration_nudge_injected",
                        consecutive_reads=consecutive_reads,
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
