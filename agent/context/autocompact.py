"""Layer 3: Proactive full conversation summarization.

When token usage approaches the context window, this engine calls the LLM
to summarize the conversation into a compact boundary message. Includes a
circuit breaker to prevent death spirals on repeated failures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from agent.llm.types import LLMResponse, Message, TokenUsage

if TYPE_CHECKING:
    from agent.llm.base import LLMProvider
    from agent.context.token_counter import TokenCounter

logger = structlog.get_logger()

# Reserve for the summary output itself
SUMMARY_BUFFER = 20_000
# Fire autocompact when this close to the effective limit
TRIGGER_BUFFER = 13_000
# Don't compact tiny conversations
MIN_MESSAGES = 5
MIN_TOKENS = 10_000
# Circuit breaker
MAX_CONSECUTIVE_FAILURES = 3

SUMMARY_SYSTEM_PROMPT = """\
You are a context summarizer. Summarize the conversation below into structured \
markdown that preserves everything needed to continue the work.

Your summary MUST include:
1. **Primary request**: What the user asked for and why.
2. **Technical decisions**: Key choices made (libraries, patterns, approaches).
3. **Files changed**: Every file that was read, created, or modified — with a one-line note on what was done.
4. **Errors & fixes**: Any errors encountered and how they were resolved.
5. **Current status**: Where the work stands right now.
6. **Next steps**: What remains to be done.
7. **User messages**: All user instructions (chronological, verbatim where important).

Be comprehensive but concise. This summary replaces the full history — anything \
you omit is lost permanently."""


class AutocompactEngine:
    """Proactively summarizes the conversation when approaching context limits."""

    def __init__(self, provider: LLMProvider, counter: TokenCounter) -> None:
        self._provider = provider
        self._counter = counter
        self._failure_count = 0
        self._disabled = False

    @property
    def is_disabled(self) -> bool:
        return self._disabled

    async def maybe_compact(
        self,
        messages: list[Message],
        current_tokens: int,
    ) -> tuple[list[Message], bool]:
        """Check if compaction is needed and perform it if so.

        Returns:
            (messages, did_compact) — possibly shortened message list.
        """
        if self._disabled:
            return messages, False

        threshold = self._provider.max_context_tokens - SUMMARY_BUFFER - TRIGGER_BUFFER
        if current_tokens < threshold:
            return messages, False

        if len(messages) < MIN_MESSAGES:
            return messages, False

        if current_tokens < MIN_TOKENS:
            return messages, False

        logger.info(
            "autocompact_triggered",
            current_tokens=current_tokens,
            threshold=threshold,
            message_count=len(messages),
        )

        try:
            summary = await self._summarize(messages)
            boundary = Message(
                role="user",
                content=f"[Context Summary — earlier conversation was compacted]\n\n{summary}",
                token_estimate=self._counter.rough_estimate(summary),
            )
            self._failure_count = 0
            return [boundary], True

        except Exception as e:
            self._failure_count += 1
            logger.error(
                "autocompact_failed",
                error=str(e),
                failure_count=self._failure_count,
            )
            if self._failure_count >= MAX_CONSECUTIVE_FAILURES:
                logger.error("autocompact_circuit_breaker_tripped")
                self._disabled = True
            return messages, False

    async def force_compact(self, messages: list[Message]) -> list[Message]:
        """Force compaction regardless of thresholds (used by reactive compact)."""
        try:
            summary = await self._summarize(messages)
            boundary = Message(
                role="user",
                content=f"[Context Summary — earlier conversation was compacted]\n\n{summary}",
                token_estimate=self._counter.rough_estimate(summary),
            )
            self._failure_count = 0
            return [boundary]
        except Exception:
            raise

    async def _summarize(self, messages: list[Message]) -> str:
        """Call the LLM to produce a conversation summary."""
        # Build a condensed version of the conversation for summarization
        conversation_text = self._format_for_summary(messages)

        response = await self._provider.complete(
            messages=[Message(role="user", content=conversation_text)],
            system=SUMMARY_SYSTEM_PROMPT,
            max_tokens=8192,
            temperature=0.0,
        )
        return response.message.content

    def _format_for_summary(self, messages: list[Message]) -> str:
        """Format messages into a readable text for the summarizer."""
        parts: list[str] = []
        for msg in messages:
            prefix = msg.role.upper()
            if msg.role == "tool":
                tool_name = msg.tool_name or "unknown"
                # Truncate large tool results
                content = msg.content[:2000] if len(msg.content) > 2000 else msg.content
                parts.append(f"[TOOL:{tool_name}] {content}")
            elif msg.role == "assistant" and msg.tool_calls:
                tool_names = ", ".join(tc.name for tc in msg.tool_calls)
                text = msg.content[:1000] if msg.content else ""
                parts.append(f"ASSISTANT (called: {tool_names}): {text}")
            else:
                parts.append(f"{prefix}: {msg.content}")

        return "\n\n".join(parts)
