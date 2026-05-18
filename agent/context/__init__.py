"""Context management orchestrator — runs all compaction layers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from agent.context.attachments import AttachmentRestorer
from agent.context.autocompact import AutocompactEngine
from agent.context.context_collapse import ContextCollapseEngine
from agent.context.memory import query_relevant_memory
from agent.context.microcompact import MicrocompactEngine
from agent.context.reactive_compact import PromptTooLongError, ReactiveCompactEngine
from agent.context.system import SystemPromptBuilder
from agent.context.token_counter import TokenCounter

if TYPE_CHECKING:
    from agent.llm.base import LLMProvider
    from agent.llm.types import Message, ToolDefinition

logger = structlog.get_logger()


class ContextManager:
    """Orchestrates all context layers. Single entry point for the AgentLoop.

    Layer pipeline (run before every LLM call):
        1. Microcompact — clear stale tool results (always runs, cheap)
        2. Context collapse — group read/search ops into summaries
        3. Autocompact — proactive summarization when approaching limit

    Error recovery (on prompt-too-long):
        4. Reactive compact — 3-stage escalation
    """

    def __init__(self, workspace: str, provider: LLMProvider) -> None:
        self._workspace = workspace
        self._provider = provider

        self.counter = TokenCounter(provider)
        self.microcompact = MicrocompactEngine()
        self.collapse = ContextCollapseEngine()
        self.autocompact = AutocompactEngine(provider, self.counter)
        self.reactive = ReactiveCompactEngine(provider, self.counter)
        self.attachments = AttachmentRestorer(self.counter, workspace)
        self.system = SystemPromptBuilder()

    async def build_system_prompt(
        self,
        repo_summary: str | None = None,
        extra_instructions: str | None = None,
        include_methodology: bool = False,
        task_description: str | None = None,
        repo_name: str | None = None,
        repo_id: int | None = None,
    ) -> str:
        """Build the full system prompt for this workspace.

        ``repo_id`` enables the ADR-016 Phase 6 code-graph nudge — the
        builder consults the DB to check whether the repo has an active
        graph and, if so, appends a paragraph pointing at
        ``query_repo_graph``. None of the existing callers are required
        to pass it; the parameter is additive.
        """
        memory_context = None
        if task_description:
            try:
                memory_context = await query_relevant_memory(task_description)
            except Exception:
                logger.warning("memory_context_query_failed")

        return await self.system.build(
            self._workspace,
            repo_summary=repo_summary,
            extra_instructions=extra_instructions,
            include_methodology=include_methodology,
            memory_context=memory_context,
            repo_name=repo_name,
            repo_id=repo_id,
        )

    async def prepare(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
    ) -> list[Message]:
        """Run the compaction pipeline before an LLM call.

        Returns an optimized message list ready for the API.
        """
        original_messages = messages

        # Layer 1: Microcompact (clear old tool results)
        messages = self.microcompact.apply(messages, self._provider.max_context_tokens)

        # Layer 2: Context collapse (group read/search ops)
        messages = self.collapse.apply(messages)

        # Layer 3: Autocompact (proactive summarization)
        current_tokens = await self.counter.count(messages, system, tools)
        messages, did_compact = await self.autocompact.maybe_compact(messages, current_tokens)

        if did_compact:
            messages = await self.attachments.restore(messages, original_messages)
            logger.info(
                "context_compacted",
                before_count=len(original_messages),
                after_count=len(messages),
            )

        return messages

    async def handle_prompt_too_long(
        self, messages: list[Message]
    ) -> list[Message]:
        """Handle a prompt-too-long error with staged recovery.

        Raises PromptTooLongError if all stages fail.
        """
        return await self.reactive.handle_prompt_too_long(
            messages, self.collapse, self.autocompact
        )

    def invalidate_cache(self) -> None:
        """Clear cached system prompt values."""
        self.system.invalidate_cache()
