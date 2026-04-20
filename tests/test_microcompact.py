"""Tests for agent/context/microcompact.py — tool result clearing.

Critical invariant: the agent's working memory of file contents must NOT
be cleared unless a more recent read of the same file supersedes it.
Erasing file_read results causes the agent to re-read files and appear stuck.
"""

from agent.context.microcompact import CLEARED_MARKER, MicrocompactEngine
from agent.llm.types import Message, ToolCall


def _read_turn(call_id: str, file_path: str, content: str) -> tuple[Message, Message]:
    """Build an assistant message with a file_read tool call + its tool result."""
    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id=call_id, name="file_read", arguments={"file_path": file_path})],
    )
    tool_result = Message(
        role="tool",
        content=content,
        tool_call_id=call_id,
        tool_name="file_read",
    )
    return assistant, tool_result


def _grep_turn(call_id: str, pattern: str, content: str) -> tuple[Message, Message]:
    """Build an assistant message with a grep tool call + its tool result."""
    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id=call_id, name="grep", arguments={"pattern": pattern})],
    )
    tool_result = Message(
        role="tool",
        content=content,
        tool_call_id=call_id,
        tool_name="grep",
    )
    return assistant, tool_result


class TestFileReadPreservation:
    """The critical fix: file_read results must survive unless superseded."""

    def test_single_read_is_preserved_indefinitely(self):
        """A file read once is never cleared — it's the agent's only copy."""
        engine = MicrocompactEngine()
        big_content = "x" * 500  # Over the 200-char threshold

        # 10 turns of grep calls, but only 1 file_read at the start
        messages = []
        a, r = _read_turn("read_1", "app.py", big_content)
        messages.extend([a, r])

        # Many subsequent turns doing other things
        for i in range(10):
            a, r = _grep_turn(f"grep_{i}", "foo", "match " * 50)
            messages.extend([a, r])

        # Final user message
        messages.append(Message(role="user", content="continue"))

        result = engine.apply(messages, max_context_tokens=200_000)

        # Find the file_read result — it should still have full content
        read_results = [m for m in result if m.tool_name == "file_read"]
        assert len(read_results) == 1
        assert read_results[0].content == big_content, (
            "file_read content was cleared even though no newer read exists — "
            "this is the bug that traps the agent in a re-read loop"
        )

    def test_two_reads_of_different_files_both_preserved(self):
        """Reading two different files — both should be preserved."""
        engine = MicrocompactEngine()

        messages = []
        a1, r1 = _read_turn("r1", "app.py", "app content " * 50)
        a2, r2 = _read_turn("r2", "models.py", "models content " * 50)
        messages.extend([a1, r1, a2, r2])

        # Many grep/assistant turns after
        for i in range(8):
            a, r = _grep_turn(f"g{i}", "x", "y " * 50)
            messages.extend([a, r])

        result = engine.apply(messages, max_context_tokens=200_000)

        read_results = {m.tool_call_id: m.content for m in result if m.tool_name == "file_read"}
        assert "app content" in read_results.get("r1", ""), "app.py read was cleared"
        assert "models content" in read_results.get("r2", ""), "models.py read was cleared"

    def test_duplicate_reads_both_preserved(self):
        """Re-reads of the same file are BOTH preserved.

        Clearing older reads (even with a 'superseded' marker) confuses the
        agent into thinking it lost its memory and drives a re-read loop.
        """
        engine = MicrocompactEngine()

        old_content = "old version " * 30
        new_content = "new version " * 30

        messages = []
        a1, r1 = _read_turn("r_old", "app.py", old_content)
        messages.extend([a1, r1])

        # Many turns of other work (old read is well past keep-recent window)
        for i in range(5):
            a, r = _grep_turn(f"g{i}", "x", "result " * 40)
            messages.extend([a, r])

        # Read app.py again
        a2, r2 = _read_turn("r_new", "app.py", new_content)
        messages.extend([a2, r2])

        # More turns past keep-recent
        for i in range(5):
            a, r = _grep_turn(f"g2_{i}", "y", "x " * 40)
            messages.extend([a, r])

        messages.append(Message(role="user", content="continue"))

        result = engine.apply(messages, max_context_tokens=200_000)

        old_result = next(m for m in result if m.tool_call_id == "r_old")
        new_result = next(m for m in result if m.tool_call_id == "r_new")

        assert old_result.content == old_content, "Old read must be preserved (no superseding)"
        assert new_result.content == new_content, "New read must be preserved"


class TestComputedToolClearing:
    """Computed tools (grep/glob/bash/git) are still cleared — their results
    can be re-derived and they're not the agent's working memory."""

    def test_old_grep_is_cleared(self):
        engine = MicrocompactEngine()

        messages = []
        # Old grep that should be cleared
        a, r = _grep_turn("old_grep", "TODO", "many matches " * 40)
        messages.extend([a, r])

        # Many turns after
        for i in range(8):
            a, r = _grep_turn(f"g{i}", "foo", "x " * 40)
            messages.extend([a, r])
        messages.append(Message(role="user", content="continue"))

        result = engine.apply(messages, max_context_tokens=200_000)

        old_result = next(m for m in result if m.tool_call_id == "old_grep")
        assert old_result.content == CLEARED_MARKER

    def test_recent_results_preserved(self):
        """KEEP_RECENT_TURNS protects the most recent tool results."""
        engine = MicrocompactEngine()

        messages = []
        a, r = _grep_turn("recent_grep", "foo", "recent matches " * 40)
        messages.extend([a, r])
        messages.append(Message(role="user", content="what now?"))

        result = engine.apply(messages, max_context_tokens=200_000)

        recent_result = next(m for m in result if m.tool_call_id == "recent_grep")
        assert recent_result.content != CLEARED_MARKER


class TestEdgeCases:
    def test_short_results_not_cleared(self):
        """Tiny tool results (<200 chars) are left alone regardless of age."""
        engine = MicrocompactEngine()

        messages = []
        # Tiny grep that's old
        a = Message(role="assistant", content="", tool_calls=[
            ToolCall(id="tiny", name="grep", arguments={"pattern": "x"})
        ])
        r = Message(role="tool", content="no matches", tool_call_id="tiny", tool_name="grep")
        messages.extend([a, r])

        # Push it past the cutoff
        for i in range(8):
            a, r = _grep_turn(f"g{i}", "y", "match " * 40)
            messages.extend([a, r])
        messages.append(Message(role="user", content="continue"))

        result = engine.apply(messages, max_context_tokens=200_000)

        tiny_result = next(m for m in result if m.tool_call_id == "tiny")
        assert tiny_result.content == "no matches", "Short results should not be cleared"

    def test_short_conversation_not_compacted(self):
        """If there are fewer than KEEP_RECENT_TURNS assistant messages, do nothing."""
        engine = MicrocompactEngine()

        a, r = _read_turn("r1", "app.py", "content " * 50)
        messages = [a, r]

        result = engine.apply(messages, max_context_tokens=200_000)
        read_result = next(m for m in result if m.tool_call_id == "r1")
        assert "content" in read_result.content
