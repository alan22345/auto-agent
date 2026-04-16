"""Tests for agent/context/context_collapse.py.

Critical invariant: file_read results must NEVER be collapsed. Collapsing
them destroys the agent's working memory of file contents and causes the
agent to re-read files in a stuck loop.
"""

from agent.context.context_collapse import COLLAPSIBLE_TOOLS, ContextCollapseEngine
from agent.llm.types import Message, ToolCall


def _assistant(call_ids_tools_args: list[tuple[str, str, dict]]) -> Message:
    return Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id=i, name=n, arguments=a) for i, n, a in call_ids_tools_args],
    )


def _tool_result(call_id: str, name: str, content: str) -> Message:
    return Message(role="tool", content=content, tool_call_id=call_id, tool_name=name)


class TestFileReadNotCollapsed:
    """The critical invariant: file_read is NOT in COLLAPSIBLE_TOOLS."""

    def test_file_read_excluded_from_collapsible(self):
        assert "file_read" not in COLLAPSIBLE_TOOLS

    def test_file_reads_preserved_verbatim(self):
        """Even 10 file_reads in a row must be preserved fully."""
        engine = ContextCollapseEngine()

        messages = [Message(role="user", content="do it")]
        for i in range(10):
            call_id = f"r{i}"
            path = f"file_{i}.py"
            content = f"# File {i}\nclass Widget{i}: pass\n" * 20  # ~600 chars
            messages.append(_assistant([(call_id, "file_read", {"file_path": path})]))
            messages.append(_tool_result(call_id, "file_read", content))

        result = engine.apply(messages)

        # All 10 file reads should still be there
        read_results = [m for m in result if m.tool_name == "file_read"]
        assert len(read_results) == 10, "No file reads should be collapsed"

        # Content should be intact
        for i, msg in enumerate(read_results):
            assert f"Widget" in msg.content, f"File read {i} content was altered"

        # Assistant tool calls should also be intact
        assistant_with_reads = [
            m for m in result
            if m.role == "assistant" and m.tool_calls
            and any(tc.name == "file_read" for tc in m.tool_calls)
        ]
        assert len(assistant_with_reads) == 10


class TestComputedToolsStillCollapsed:
    """grep/glob/git can still be collapsed — they're re-derivable."""

    def test_many_greps_collapsed(self):
        engine = ContextCollapseEngine()
        messages = [Message(role="user", content="search")]
        for i in range(5):
            messages.append(_assistant([(f"g{i}", "grep", {"pattern": f"p{i}"})]))
            messages.append(_tool_result(f"g{i}", "grep", f"many matches {i} " * 20))

        result = engine.apply(messages)

        # At least some collapse should have happened (fewer messages than input)
        grep_results = [m for m in result if m.tool_name == "grep"]
        # Either the results are gone (collapsed) or reduced
        assert len(grep_results) < 5 or any("[Collapsed" in m.content for m in result)

    def test_globs_collapsed(self):
        engine = ContextCollapseEngine()
        messages = [Message(role="user", content="find")]
        for i in range(4):
            messages.append(_assistant([(f"gl{i}", "glob", {"pattern": f"**/*.{i}"})]))
            messages.append(_tool_result(f"gl{i}", "glob", f"file_{i}.py\n" * 30))
        result = engine.apply(messages)
        glob_results = [m for m in result if m.tool_name == "glob"]
        assert len(glob_results) < 4 or any("[Collapsed" in m.content for m in result)


class TestMixedOperations:
    """When file_reads and grep are mixed, only grep should be collapsed."""

    def test_mixed_grep_and_reads_preserves_reads(self):
        engine = ContextCollapseEngine()
        messages = [
            Message(role="user", content="task"),
            # Assistant does a mix: read a file, grep for pattern, read another file
            _assistant([
                ("a", "file_read", {"file_path": "app.py"}),
                ("b", "grep", {"pattern": "class"}),
                ("c", "file_read", {"file_path": "models.py"}),
            ]),
            _tool_result("a", "file_read", "def main():\n    pass\n" * 20),
            _tool_result("b", "grep", "match1\nmatch2\n" * 20),
            _tool_result("c", "file_read", "class User:\n    pass\n" * 20),
        ]

        result = engine.apply(messages)

        # A turn with mixed tools shouldn't be collapsed (not ALL collapsible)
        # because our logic requires all_collapsible in identify_groups
        # Assert: read contents are intact
        read_contents = [m.content for m in result if m.tool_name == "file_read"]
        assert any("def main" in c for c in read_contents)
        assert any("class User" in c for c in read_contents)

    def test_pure_read_turn_not_collapsed(self):
        """A turn with only file_reads is NOT collapsible (file_read not in set)."""
        engine = ContextCollapseEngine()
        messages = [
            Message(role="user", content="task"),
            _assistant([
                ("a", "file_read", {"file_path": "a.py"}),
                ("b", "file_read", {"file_path": "b.py"}),
                ("c", "file_read", {"file_path": "c.py"}),
                ("d", "file_read", {"file_path": "d.py"}),
            ]),
            _tool_result("a", "file_read", "a content " * 30),
            _tool_result("b", "file_read", "b content " * 30),
            _tool_result("c", "file_read", "c content " * 30),
            _tool_result("d", "file_read", "d content " * 30),
        ]

        result = engine.apply(messages)
        # All 4 file_read results should be present with content
        reads = [m for m in result if m.tool_name == "file_read"]
        assert len(reads) == 4
        for msg in reads:
            assert "content" in msg.content


class TestDrainAll:
    """drain_all forces collapse — but must still not destroy file_reads."""

    def test_drain_all_preserves_file_reads(self):
        engine = ContextCollapseEngine()
        messages = [Message(role="user", content="task")]
        for i in range(5):
            messages.append(_assistant([(f"r{i}", "file_read", {"file_path": f"f{i}.py"})]))
            messages.append(_tool_result(f"r{i}", "file_read", f"file {i} contents " * 30))

        result = engine.drain_all(messages)
        reads = [m for m in result if m.tool_name == "file_read"]
        # file_read is not collapsible, so drain_all should preserve them all
        assert len(reads) == 5
