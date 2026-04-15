"""Layer 1: Clear old tool results to free tokens cheaply.

This is the cheapest compaction layer. It replaces old tool result content
with a placeholder without summarizing the conversation. Runs every turn.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.llm.types import Message

# Tool results eligible for clearing (read/search operations)
ELIGIBLE_TOOLS = {"file_read", "grep", "glob", "git", "bash"}

# Never clear results from the most recent N assistant turns
KEEP_RECENT_TURNS = 3

# Placeholder text for cleared results
CLEARED_MARKER = "[Tool result cleared — content was processed in earlier turn]"


class MicrocompactEngine:
    """Replaces old tool result content with a short placeholder."""

    def apply(self, messages: list[Message], max_context_tokens: int) -> list[Message]:
        """Walk messages oldest-to-newest, clearing eligible tool results
        that are older than KEEP_RECENT_TURNS assistant turns.

        Returns a new list (does not mutate the originals).
        """
        # Identify the index of the Nth-most-recent assistant message
        assistant_indices: list[int] = []
        for i, msg in enumerate(messages):
            if msg.role == "assistant":
                assistant_indices.append(i)

        if len(assistant_indices) <= KEEP_RECENT_TURNS:
            return messages  # Not enough history to compact

        cutoff_index = assistant_indices[-KEEP_RECENT_TURNS]

        result: list[Message] = []
        for i, msg in enumerate(messages):
            if (
                i < cutoff_index
                and msg.role == "tool"
                and msg.tool_name in ELIGIBLE_TOOLS
                and msg.content != CLEARED_MARKER
                and len(msg.content) > 200  # Don't bother clearing tiny results
            ):
                cleared = copy.copy(msg)
                cleared.content = CLEARED_MARKER
                cleared.token_estimate = 10
                result.append(cleared)
            else:
                result.append(msg)

        return result
