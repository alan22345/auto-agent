"""Layer 1: Clear old tool results to free tokens cheaply.

This is the cheapest compaction layer. It replaces old tool result content
with a placeholder without summarizing the conversation. Runs every turn.

Design principle: file_read results are the agent's working memory of the
codebase. Clearing them — even with a helpful "superseded" marker — confuses
the agent into thinking it has amnesia and drives it into a re-read loop.
We therefore ONLY clear COMPUTED tool results (grep/glob/git/bash), which
are trivially re-derivable. If the agent re-reads the same file, that's
wasteful but not catastrophic — autocompact handles it at the context
boundary.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.llm.types import Message

# Tool results whose content is derived/computed and can be safely cleared.
# The agent can re-derive them from the repo map or other context.
COMPUTED_TOOLS = {"grep", "glob", "git", "bash"}

# Never clear results from the most recent N assistant turns
KEEP_RECENT_TURNS = 3

# Placeholder text for cleared results
CLEARED_MARKER = "[Tool result cleared — content was processed in earlier turn]"


class MicrocompactEngine:
    """Clears old results from computed tools (grep/glob/git/bash).

    Does NOT clear file_read results — those are the agent's working memory
    of the codebase. Clearing file reads causes the agent to re-read files
    in a stuck loop. Token efficiency for reads is handled by autocompact.
    """

    def apply(self, messages: list[Message], max_context_tokens: int) -> list[Message]:
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
                and msg.tool_name in COMPUTED_TOOLS
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
