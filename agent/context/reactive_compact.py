"""Layer 4: Error recovery compaction for prompt-too-long errors.

3-stage escalation:
1. Drain context collapses (cheapest)
2. Force autocompact with aggressive truncation
3. Surface error (unrecoverable)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from agent.context.autocompact import AutocompactEngine
    from agent.context.context_collapse import ContextCollapseEngine
    from agent.context.token_counter import TokenCounter
    from agent.llm.base import LLMProvider
    from agent.llm.types import Message

logger = structlog.get_logger()


class PromptTooLongError(Exception):
    """Raised when all recovery attempts are exhausted."""


class ReactiveCompactEngine:
    """Handles prompt-too-long errors with staged recovery."""

    def __init__(self, provider: LLMProvider, counter: TokenCounter) -> None:
        self._provider = provider
        self._counter = counter

    async def handle_prompt_too_long(
        self,
        messages: list[Message],
        collapse_engine: ContextCollapseEngine,
        autocompact_engine: AutocompactEngine,
    ) -> list[Message]:
        """3-stage escalation to recover from prompt-too-long errors.

        Raises PromptTooLongError if all stages fail.
        """
        # Stage 1: Drain all context collapses (cheapest)
        logger.info("reactive_compact_stage1", message_count=len(messages))
        drained = collapse_engine.drain_all(messages)
        drained_tokens = self._counter.estimate_messages(drained)

        if drained_tokens < self._provider.max_context_tokens * 0.85:
            logger.info("reactive_compact_stage1_success", tokens=drained_tokens)
            return drained

        # Stage 2: Force autocompact
        logger.info("reactive_compact_stage2", tokens=drained_tokens)
        for attempt in range(3):
            try:
                msgs_to_compact = drained if attempt == 0 else self._truncate_oldest(drained, attempt)
                compacted = await autocompact_engine.force_compact(msgs_to_compact)
                logger.info("reactive_compact_stage2_success", attempt=attempt + 1)
                return compacted
            except Exception as e:
                logger.warning(
                    "reactive_compact_attempt_failed",
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt == 2:
                    break

        # Stage 3: Surface error
        logger.error("reactive_compact_all_stages_failed")
        raise PromptTooLongError(
            "Conversation is too large to recover. All compaction attempts failed."
        )

    def _truncate_oldest(self, messages: list[Message], attempt: int) -> list[Message]:
        """Drop oldest message groups to reduce size for compaction.

        attempt=1: drop ~20% of messages
        attempt=2: drop ~50% of messages
        """
        if not messages:
            return messages

        drop_pct = 0.2 if attempt == 1 else 0.5
        drop_count = max(1, int(len(messages) * drop_pct))

        # Always keep at least the last few messages
        keep_minimum = 3
        if len(messages) - drop_count < keep_minimum:
            drop_count = max(0, len(messages) - keep_minimum)

        truncated = messages[drop_count:]

        # Prepend a marker so the summary knows history was lost
        from agent.llm.types import Message as Msg

        marker = Msg(
            role="user",
            content=(
                f"[Note: {drop_count} earlier messages were dropped to fit context limits. "
                "Some history has been lost.]"
            ),
        )
        return [marker] + truncated
