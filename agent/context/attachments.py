"""Post-compaction context restoration.

After autocompact summarizes the conversation, re-inject critical context
that would otherwise be lost: recently-read files, active plans, etc.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from agent.context.token_counter import TokenCounter
    from agent.llm.types import Message

logger = structlog.get_logger()

# Budgets
MAX_FILES = 5
FILE_BUDGET_TOKENS = 50_000
MAX_CHARS_PER_FILE = 30_000


class AttachmentRestorer:
    """Re-injects critical context after compaction."""

    def __init__(self, counter: TokenCounter, workspace: str) -> None:
        self._counter = counter
        self._workspace = workspace

    async def restore(
        self,
        compacted_messages: list[Message],
        original_messages: list[Message],
    ) -> list[Message]:
        """Re-inject recently-read files and active plan into the compacted messages."""
        from agent.llm.types import Message as Msg

        attachments: list[Message] = []
        token_budget = FILE_BUDGET_TOKENS

        # 1. Restore recently-read files
        recent_files = self._extract_recent_files(original_messages)
        files_in_summary = self._files_mentioned_in(compacted_messages)

        for file_path in recent_files[:MAX_FILES]:
            if file_path in files_in_summary:
                continue

            full_path = os.path.join(self._workspace, file_path)
            if not os.path.isfile(full_path):
                continue

            try:
                with open(full_path, "r", errors="replace") as f:
                    content = f.read(MAX_CHARS_PER_FILE)
            except Exception:
                continue

            tokens = self._counter.rough_estimate(content)
            if tokens > token_budget:
                continue
            token_budget -= tokens

            attachments.append(
                Msg(
                    role="user",
                    content=f"[Re-injected file: {file_path}]\n```\n{content}\n```",
                    token_estimate=tokens,
                )
            )

        # 2. Restore active plan if found
        plan_content = self._extract_active_plan(original_messages)
        if plan_content:
            attachments.append(
                Msg(
                    role="user",
                    content=f"[Active plan from before compaction]\n{plan_content}",
                    token_estimate=self._counter.rough_estimate(plan_content),
                )
            )

        if attachments:
            logger.info("attachments_restored", count=len(attachments))

        return compacted_messages + attachments

    def _extract_recent_files(self, messages: list[Message]) -> list[str]:
        """Get unique file paths from recent file_read tool calls, newest first."""
        seen: set[str] = set()
        result: list[str] = []

        for msg in reversed(messages):
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.name == "file_read":
                        fp = tc.arguments.get("file_path", "")
                        if fp and fp not in seen:
                            seen.add(fp)
                            result.append(fp)
                            if len(result) >= MAX_FILES * 2:
                                return result
        return result

    def _files_mentioned_in(self, messages: list[Message]) -> set[str]:
        """Rough check for file paths already present in message content."""
        mentioned: set[str] = set()
        for msg in messages:
            if msg.content:
                # Simple heuristic — check if the filename appears in the summary
                mentioned.update(
                    word for word in msg.content.split()
                    if "/" in word and "." in word.split("/")[-1]
                )
        return mentioned

    def _extract_active_plan(self, messages: list[Message]) -> str | None:
        """Look for an active plan in the conversation (from planning phase)."""
        for msg in reversed(messages):
            if msg.role == "assistant" and msg.content:
                content = msg.content
                if "## plan" in content.lower() or "## implementation plan" in content.lower():
                    # Truncate to a reasonable size
                    if len(content) > 5000:
                        content = content[:5000] + "\n... (plan truncated)"
                    return content
        return None
