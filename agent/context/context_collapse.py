"""Layer 2: Group consecutive read/search operations into summaries.

Creates a projected view of messages without modifying stored history.
Consecutive read-only tool calls are collapsed into compact summaries.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.llm.types import Message

# Tools whose results can be collapsed into group summaries
COLLAPSIBLE_TOOLS = {"file_read", "grep", "glob", "git"}

# Tools that break a collapsible group (mutations)
BREAK_TOOLS = {"file_write", "file_edit", "bash"}


class ContextCollapseEngine:
    """Groups consecutive read/search operations into compact summaries."""

    def apply(self, messages: list[Message]) -> list[Message]:
        """Return a projected message list with collapsible groups summarized.

        Does NOT modify the input list. Groups of consecutive tool_use +
        tool_result pairs for read-only tools are replaced by a single
        summary message.
        """
        groups = self._identify_groups(messages)
        if not groups:
            return messages

        result: list[Message] = []
        skip_indices: set[int] = set()

        for start, end in groups:
            # Only collapse if the group has 3+ tool calls (worth summarizing)
            tool_count = sum(
                1 for i in range(start, end + 1) if messages[i].role == "tool"
            )
            if tool_count < 3:
                continue
            for i in range(start, end + 1):
                skip_indices.add(i)
            summary = self._summarize_group(messages[start : end + 1])
            # Insert summary at the group's position
            result.append(summary)

        # Build final list preserving order
        if not skip_indices:
            return messages

        final: list[Message] = []
        group_starts = {g[0] for g in groups if any(i in skip_indices for i in range(g[0], g[1] + 1))}
        summary_map = {}
        idx = 0
        for start, end in groups:
            if start in group_starts and any(i in skip_indices for i in range(start, end + 1)):
                summary_map[start] = self._summarize_group(messages[start : end + 1])

        for i, msg in enumerate(messages):
            if i in skip_indices:
                if i in summary_map:
                    final.append(summary_map[i])
                continue
            final.append(msg)

        return final

    def drain_all(self, messages: list[Message]) -> list[Message]:
        """Force-collapse all possible groups. Used as cheap recovery
        step before expensive reactive compaction."""
        groups = self._identify_groups(messages)
        if not groups:
            return messages

        skip_indices: set[int] = set()
        summary_map: dict[int, Message] = {}

        for start, end in groups:
            tool_count = sum(1 for i in range(start, end + 1) if messages[i].role == "tool")
            if tool_count < 2:  # Lower threshold for drain
                continue
            for i in range(start, end + 1):
                skip_indices.add(i)
            summary_map[start] = self._summarize_group(messages[start : end + 1])

        if not skip_indices:
            return messages

        final: list[Message] = []
        for i, msg in enumerate(messages):
            if i in skip_indices:
                if i in summary_map:
                    final.append(summary_map[i])
                continue
            final.append(msg)
        return final

    def _identify_groups(self, messages: list[Message]) -> list[tuple[int, int]]:
        """Find runs of consecutive collapsible assistant+tool message pairs."""
        groups: list[tuple[int, int]] = []
        current_start: int | None = None

        i = 0
        while i < len(messages):
            msg = messages[i]

            if msg.role == "assistant" and msg.tool_calls:
                # Check if all tool calls in this turn are collapsible
                all_collapsible = all(
                    tc.name in COLLAPSIBLE_TOOLS for tc in msg.tool_calls
                )
                if all_collapsible:
                    if current_start is None:
                        current_start = i
                    # Skip past the tool results
                    i += 1
                    while i < len(messages) and messages[i].role == "tool":
                        i += 1
                    continue
                else:
                    # Break point — mutation tool
                    if current_start is not None:
                        groups.append((current_start, i - 1))
                        current_start = None

            elif msg.role in ("user", "system"):
                # User messages break groups
                if current_start is not None:
                    groups.append((current_start, i - 1))
                    current_start = None

            i += 1

        if current_start is not None:
            groups.append((current_start, len(messages) - 1))

        return groups

    def _summarize_group(self, group_messages: list[Message]) -> Message:
        """Create a summary message for a group of read/search operations."""
        from agent.llm.types import Message as Msg

        files_read: set[str] = set()
        patterns_searched: set[str] = set()
        git_commands: list[str] = []

        for msg in group_messages:
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    if tc.name == "file_read":
                        fp = tc.arguments.get("file_path", "?")
                        files_read.add(fp)
                    elif tc.name == "grep":
                        pat = tc.arguments.get("pattern", "?")
                        patterns_searched.add(pat)
                    elif tc.name == "glob":
                        pat = tc.arguments.get("pattern", "?")
                        patterns_searched.add(f"glob:{pat}")
                    elif tc.name == "git":
                        cmd = tc.arguments.get("command", "?")
                        git_commands.append(cmd)

        parts: list[str] = []
        if files_read:
            file_list = ", ".join(sorted(files_read)[:10])
            if len(files_read) > 10:
                file_list += f", ... ({len(files_read)} total)"
            parts.append(f"Read {len(files_read)} files: {file_list}")
        if patterns_searched:
            parts.append(f"Searched for: {', '.join(sorted(patterns_searched)[:5])}")
        if git_commands:
            parts.append(f"Git: {', '.join(git_commands[:5])}")

        summary = "[Collapsed exploration] " + "; ".join(parts) if parts else "[Collapsed exploration block]"

        return Msg(
            role="user",
            content=summary,
            token_estimate=len(summary) // 4,
        )
