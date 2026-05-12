"""Core agentic conversation loop.

Handles the multi-turn cycle: prompt -> LLM response -> tool execution -> continue.
Supports pass-through mode for CLI providers that manage their own tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

import structlog

from agent.context import ContextManager
from agent.context.reactive_compact import PromptTooLongError
from agent.context.workspace_state import WorkspaceState
from agent.llm.types import Message, TokenUsage, ToolCall
from agent.session import Session
from agent.tools.base import ToolContext, ToolRegistry, ToolResult
from agent.tools.cache import ToolCache
from shared.quotas import QuotaExceeded

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

_COMPLEXITY_BUDGETS = {
    "simple": 5,
    "complex": 15,
    "complex_large": 25,
}


def get_exploration_budget(complexity: str | None) -> int:
    """Return the exploration budget for a given task complexity."""
    if complexity is None:
        return _EXPLORATION_BUDGET
    return _COMPLEXITY_BUDGETS.get(complexity, _EXPLORATION_BUDGET)

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
class UsageSink:
    """Per-task accounting helper. Injected into AgentLoop.

    `emit` writes one row to usage_events (best-effort).
    `would_exceed_token_cap` returns True when the next call would cross
    the org's daily input/output token caps.

    Pass *db_session* only in tests or callers that already hold a transaction
    and want the row flushed into that transaction rather than a new session.
    """

    org_id: int
    task_id: int | None = None
    db_session: object | None = None  # AsyncSession | None — typed as object to avoid import at top level

    async def emit(self, *, model: str, usage: TokenUsage) -> None:
        from shared.usage import emit_usage_event

        await emit_usage_event(
            org_id=self.org_id,
            task_id=self.task_id,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            session=self.db_session,  # type: ignore[arg-type]
        )

    async def would_exceed_token_cap(
        self, *, est_input: int, est_output: int
    ) -> bool:
        from shared import quotas
        from shared.database import async_session

        async with async_session() as session:
            return await quotas.would_exceed_token_cap(
                session, self.org_id,
                est_input=est_input, est_output=est_output,
            )


@dataclass
class AgentResult:
    """Final result from an agent run."""

    output: str
    tool_calls_made: int = 0
    tokens_used: TokenUsage = field(default_factory=TokenUsage)
    messages: list[Message] = field(default_factory=list)
    api_messages: list[Message] = field(default_factory=list)


@dataclass
class ToolBatchOutcome:
    """Aggregated result of executing one batch of tool calls.

    Returned by AgentLoop._process_tool_calls so the caller can update
    cross-turn state (counts, verification gate, exploration budget)
    without re-implementing the per-call bookkeeping.
    """

    count: int = 0
    turn_has_write: bool = False
    wrote: bool = False
    verified: bool = False


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
        task_description: str | None = None,
        heartbeat: Callable[[], Awaitable[None]] | None = None,
        on_tool_call: Callable[[str, dict, str, int], Awaitable[None]] | None = None,
        on_thinking: Callable[[str, int], Awaitable[None]] | None = None,
        get_guidance: Callable[[], Awaitable[str | None]] | None = None,
        repo_name: str | None = None,
        complexity: str | None = None,
        event_sink: Callable[[dict], Awaitable[None]] | None = None,
        home_dir: str | None = None,
        usage_sink: UsageSink | None = None,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._context = context_manager
        self._session = session
        self._max_turns = max_turns
        self._workspace = workspace
        self._include_methodology = include_methodology
        self._task_description = task_description
        self._repo_name = repo_name
        self._complexity = complexity
        self._heartbeat = heartbeat  # Called every few turns to signal progress
        # Pair-programming callbacks:
        self._on_tool_call = on_tool_call   # (tool_name, args, result_preview, turn) → stream to UI
        self._on_thinking = on_thinking     # (text, turn) → stream assistant thinking to UI
        self._get_guidance = get_guidance    # () → check for user guidance messages (None = no message)
        self._event_sink = event_sink       # forwarded to ToolContext so tools can emit progress events
        self._home_dir = home_dir           # per-user HOME for the CLI provider's credential vault
        self._usage_sink = usage_sink       # per-task quota gate + usage emission

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

        if isinstance(self._provider, ClaudeCLIProvider):
            self._provider.set_cwd(self._workspace)
            if self._home_dir is not None:
                self._provider.set_home_dir(self._home_dir)
            if self._session:
                self._provider.set_session(self._session.session_id, resume=resume)

        response = await self._provider.complete(
            messages=[Message(role="user", content=prompt)],
        )

        # Post-call usage accounting (best-effort).
        if self._usage_sink is not None:
            try:
                await self._usage_sink.emit(
                    model=getattr(self._provider, "model", "unknown"),
                    usage=response.usage,
                )
            except Exception:
                logger.warning("usage_sink_emit_failed_in_passthrough")

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
                task_description=self._task_description,
                repo_name=self._repo_name,
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

        tool_context = ToolContext(
            workspace=self._workspace,
            readonly=False,
            event_sink=self._event_sink,
            usage_sink=self._usage_sink,
        )
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

            # Heartbeat every 5 turns — signals to the watchdog that the
            # agent is making progress and shouldn't be timed out.
            if self._heartbeat and turn > 0 and turn % 5 == 0:
                try:
                    await self._heartbeat()
                except Exception:
                    pass  # Heartbeat failure shouldn't abort the agent

            # Run context compaction pipeline
            api_messages = await self._context.prepare(api_messages, system, tool_defs)

            # Pre-call quota gate (rough estimate based on message content).
            if self._usage_sink is not None:
                approx_in = sum(
                    len(m.content or "") for m in api_messages
                ) // 4  # 4 chars/token rough estimate
                try:
                    exceeds = await self._usage_sink.would_exceed_token_cap(
                        est_input=approx_in, est_output=8192,
                    )
                except LookupError as e:
                    raise QuotaExceeded(
                        f"Org {self._usage_sink.org_id} has no plan attached"
                    ) from e
                if exceeds:
                    raise QuotaExceeded(
                        f"Org {self._usage_sink.org_id} would exceed daily token cap"
                    )

            # Call the LLM
            try:
                response = await self._provider.complete(
                    messages=api_messages,
                    tools=tool_defs if tool_defs else None,
                    system=system,
                )
            except QuotaExceeded:
                raise  # propagate so the caller can transition to BLOCKED_ON_QUOTA
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

            # Post-call usage accounting (best-effort).
            if self._usage_sink is not None:
                try:
                    await self._usage_sink.emit(
                        model=getattr(self._provider, "model", "unknown"),
                        usage=response.usage,
                    )
                except Exception:
                    logger.warning("usage_sink_emit_failed")

            # Accumulate usage
            cumulative_usage.input_tokens += response.usage.input_tokens
            cumulative_usage.output_tokens += response.usage.output_tokens

            # Add assistant message to both histories
            messages.append(response.message)
            api_messages.append(response.message)

            # Stream assistant thinking to UI (the text part of the response)
            if self._on_thinking and response.message.content:
                try:
                    await self._on_thinking(response.message.content, turn)
                except Exception:
                    pass

            # Check for user guidance messages injected via the UI.
            # This enables pair-programming: the user can send messages
            # while the agent is working, and they appear as user messages
            # in the conversation on the next turn.
            if self._get_guidance:
                try:
                    guidance = await self._get_guidance()
                    if guidance:
                        guidance_msg = Message(role="user", content=f"[User guidance]: {guidance}")
                        messages.append(guidance_msg)
                        api_messages.append(guidance_msg)
                        logger.info("user_guidance_injected", turn=turn, preview=guidance[:100])
                except Exception:
                    pass

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
                # Execute any complete tool_use first — otherwise the next API
                # call carries an orphan tool_use that Bedrock rejects with 400
                # "tool_use ids were found without tool_result blocks". See task 51.
                if response.message.tool_calls:
                    outcome = await self._process_tool_calls(
                        turn=turn,
                        tool_calls=response.message.tool_calls,
                        messages=messages,
                        api_messages=api_messages,
                        tool_context=tool_context,
                        tool_cache=tool_cache,
                        ws_state=ws_state,
                    )
                    total_tool_calls += outcome.count
                    has_written = has_written or outcome.wrote
                    has_verified = has_verified or outcome.verified
                    logger.warning(
                        "max_tokens_with_tool_use_executed_tools",
                        tool_count=outcome.count,
                        turn=turn,
                    )

                # Inject continuation message
                continuation = Message(
                    role="user",
                    content="Output was truncated. Continue exactly where you left off.",
                )
                messages.append(continuation)
                api_messages.append(continuation)
                continue

            if response.stop_reason == "tool_use" and response.message.tool_calls:
                outcome = await self._process_tool_calls(
                    turn=turn,
                    tool_calls=response.message.tool_calls,
                    messages=messages,
                    api_messages=api_messages,
                    tool_context=tool_context,
                    tool_cache=tool_cache,
                    ws_state=ws_state,
                )
                total_tool_calls += outcome.count
                has_written = has_written or outcome.wrote
                has_verified = has_verified or outcome.verified

                # Exploration budget: track consecutive read-only turns
                if outcome.turn_has_write:
                    consecutive_reads = 0
                else:
                    tool_names = {tc.name for tc in response.message.tool_calls}
                    if tool_names <= _READ_ONLY_TOOLS:
                        consecutive_reads += 1

                # Inject nudge if budget exceeded (once)
                budget = get_exploration_budget(self._complexity)
                if consecutive_reads >= budget and not nudge_injected:
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

    async def _process_tool_calls(
        self,
        *,
        turn: int,
        tool_calls: list[ToolCall],
        messages: list[Message],
        api_messages: list[Message],
        tool_context: ToolContext,
        tool_cache: ToolCache,
        ws_state: WorkspaceState,
    ) -> ToolBatchOutcome:
        """Execute a batch of tool calls and update all per-call loop bookkeeping.

        Owns: cache lookup/put, _execute_tool, building the role="tool" Message,
        appending to both message lists, ws_state.process_tool_call, write/verify
        flag tracking, cache invalidation on writes, and (when on_tool_call is
        configured) streaming each call to the UI callback.

        Cross-turn state (consecutive_reads, nudge_injected, verification_nudge)
        and branch-specific behaviour (continuation messages) stay in the caller.
        """
        outcome = ToolBatchOutcome()

        for tc in tool_calls:
            cached = tool_cache.get(tc.name, tc.arguments)
            if cached is not None:
                result = cached
                logger.debug("tool_cache_hit", tool=tc.name)
            else:
                result = await self._execute_tool(tc, tool_context)
                tool_cache.put(tc.name, tc.arguments, result)

            tool_cache.invalidate_on_write(tc.name)

            outcome.count += 1

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

            if self._on_tool_call:
                try:
                    preview = result.output[:200] if result.output else ""
                    await self._on_tool_call(tc.name, tc.arguments, preview, turn)
                except Exception:
                    pass

            if tc.name in _WRITE_TOOLS:
                outcome.turn_has_write = True

            if tc.name in ("file_write", "file_edit"):
                outcome.wrote = True

            if tc.name == "test_runner":
                outcome.verified = True
            elif tc.name == "bash":
                cmd = tc.arguments.get("command", "")
                if any(pat in cmd for pat in _VERIFICATION_PATTERNS):
                    outcome.verified = True

            ws_state.process_tool_call(tc.name, tc.arguments)

        return outcome

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
