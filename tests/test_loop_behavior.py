"""Tests for agent loop behaviors: exploration budget, verification gate, tool caching."""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import AsyncMock

import pytest

from agent.context import ContextManager
from agent.context.workspace_state import WorkspaceState
from agent.llm.types import Message, ToolCall
from agent.loop import (
    _EXPLORATION_BUDGET,
    _EXPLORATION_NUDGE,
    _READ_ONLY_TOOLS,
    _VERIFICATION_NUDGE,
    _VERIFICATION_PATTERNS,
    _WRITE_TOOLS,
    AgentLoop,
)
from agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from agent.tools.cache import ToolCache


class TestExplorationBudgetConstants:
    def test_read_only_tools_defined(self):
        assert "file_read" in _READ_ONLY_TOOLS
        assert "glob" in _READ_ONLY_TOOLS
        assert "grep" in _READ_ONLY_TOOLS
        assert "git" in _READ_ONLY_TOOLS

    def test_write_tools_defined(self):
        assert "file_write" in _WRITE_TOOLS
        assert "file_edit" in _WRITE_TOOLS
        assert "bash" in _WRITE_TOOLS

    def test_no_overlap(self):
        assert _READ_ONLY_TOOLS.isdisjoint(_WRITE_TOOLS)

    def test_budget_is_reasonable(self):
        assert 3 <= _EXPLORATION_BUDGET <= 20

    def test_nudge_message_exists(self):
        assert len(_EXPLORATION_NUDGE) > 20
        assert "implementing" in _EXPLORATION_NUDGE.lower() or "implement" in _EXPLORATION_NUDGE.lower()


class TestVerificationGateConstants:
    def test_verification_patterns_include_common_tools(self):
        assert "pytest" in _VERIFICATION_PATTERNS
        assert "npm test" in _VERIFICATION_PATTERNS
        assert "cargo test" in _VERIFICATION_PATTERNS
        assert "go test" in _VERIFICATION_PATTERNS

    def test_verification_nudge_mentions_tests(self):
        assert "test" in _VERIFICATION_NUDGE.lower()
        assert "verification" in _VERIFICATION_NUDGE.lower()

    def test_verification_patterns_match_real_commands(self):
        # Simulate the `any(pat in cmd)` check used in the loop
        test_commands = [
            "python -m pytest tests/ -v",
            "npm test",
            "npx jest --coverage",
            "ruff check .",
            "cargo test -- --nocapture",
            "go test ./...",
        ]
        for cmd in test_commands:
            matched = any(pat in cmd for pat in _VERIFICATION_PATTERNS)
            assert matched, f"Command '{cmd}' should match a verification pattern"

    def test_non_test_commands_dont_match(self):
        non_test_commands = [
            "ls -la",
            "git status",
            "cat README.md",
            "python app.py",
            "npm install",
        ]
        for cmd in non_test_commands:
            matched = any(pat in cmd for pat in _VERIFICATION_PATTERNS)
            assert not matched, f"Command '{cmd}' should NOT match a verification pattern"


class TestExplorationBudgetLogic:
    """Test the budget tracking logic that the loop uses, without running the full loop."""

    def test_consecutive_reads_increment(self):
        consecutive_reads = 0
        # Simulate 3 turns of read-only tool calls
        for _ in range(3):
            tool_names = {"file_read"}
            turn_has_write = False
            if turn_has_write:
                consecutive_reads = 0
            elif tool_names <= _READ_ONLY_TOOLS:
                consecutive_reads += 1
        assert consecutive_reads == 3

    def test_write_resets_counter(self):
        consecutive_reads = 5
        turn_has_write = True
        if turn_has_write:
            consecutive_reads = 0
        assert consecutive_reads == 0

    def test_mixed_tools_count_as_write(self):
        """A turn with both read and write tools counts as a write turn."""
        consecutive_reads = 5
        tool_names = {"file_read", "file_edit"}
        turn_has_write = "file_edit" in _WRITE_TOOLS
        if turn_has_write:
            consecutive_reads = 0
        assert consecutive_reads == 0

    def test_budget_triggers_at_threshold(self):
        consecutive_reads = _EXPLORATION_BUDGET
        nudge_injected = False
        if consecutive_reads >= _EXPLORATION_BUDGET and not nudge_injected:
            nudge_injected = True
        assert nudge_injected

    def test_nudge_only_fires_once(self):
        nudge_count = 0
        nudge_injected = False
        for i in range(_EXPLORATION_BUDGET + 5):
            consecutive_reads = i + 1
            if consecutive_reads >= _EXPLORATION_BUDGET and not nudge_injected:
                nudge_injected = True
                nudge_count += 1
        assert nudge_count == 1


class TestVerificationGateLogic:
    """Test the verification gate logic without running the full loop."""

    def test_no_nudge_if_no_writes(self):
        has_written = False
        has_verified = False
        verification_nudge_sent = False
        should_nudge = has_written and not has_verified and not verification_nudge_sent
        assert not should_nudge

    def test_no_nudge_if_verified(self):
        has_written = True
        has_verified = True
        verification_nudge_sent = False
        should_nudge = has_written and not has_verified and not verification_nudge_sent
        assert not should_nudge

    def test_nudge_if_wrote_but_no_verification(self):
        has_written = True
        has_verified = False
        verification_nudge_sent = False
        should_nudge = has_written and not has_verified and not verification_nudge_sent
        assert should_nudge

    def test_nudge_only_fires_once(self):
        has_written = True
        has_verified = False
        verification_nudge_sent = True  # Already sent
        should_nudge = has_written and not has_verified and not verification_nudge_sent
        assert not should_nudge

    def test_test_runner_counts_as_verification(self):
        """The test_runner tool should set has_verified = True."""
        has_verified = False
        tc_name = "test_runner"
        if tc_name == "test_runner":
            has_verified = True
        assert has_verified

    def test_bash_pytest_counts_as_verification(self):
        has_verified = False
        cmd = "python -m pytest tests/ -v --tb=short"
        if any(pat in cmd for pat in _VERIFICATION_PATTERNS):
            has_verified = True
        assert has_verified


class TestComplexityAwareExplorationBudget:
    def test_default_budget_unchanged(self):
        """Default budget (no complexity) should be 8."""
        from agent.loop import _EXPLORATION_BUDGET
        assert _EXPLORATION_BUDGET == 8

    def test_get_exploration_budget_simple(self):
        from agent.loop import get_exploration_budget
        assert get_exploration_budget("simple") == 5

    def test_get_exploration_budget_complex(self):
        from agent.loop import get_exploration_budget
        assert get_exploration_budget("complex") == 15

    def test_get_exploration_budget_complex_large(self):
        from agent.loop import get_exploration_budget
        assert get_exploration_budget("complex_large") == 25

    def test_get_exploration_budget_none_returns_default(self):
        from agent.loop import get_exploration_budget
        assert get_exploration_budget(None) == 8

    def test_get_exploration_budget_unknown_returns_default(self):
        from agent.loop import get_exploration_budget
        assert get_exploration_budget("unknown") == 8


# ----------------------------------------------------------------------
# Seam: AgentLoop._process_tool_calls
# ----------------------------------------------------------------------


class _RecordingTool(Tool):
    """Tool that returns a canned result and records execution count."""

    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def __init__(self, name: str, output: str = "ok", token_estimate: int = 0):
        self.name = name
        self.description = f"recording stub for {name}"
        self._output = output
        self._token_estimate = token_estimate
        self.calls: list[dict] = []

    async def execute(self, arguments, context):
        self.calls.append(dict(arguments))
        return ToolResult(output=self._output, token_estimate=self._token_estimate)


class _NoopProvider:
    """Bare-minimum provider stub. We never call .complete() in these tests."""

    model = "stub-model"
    max_context_tokens = 200_000
    is_passthrough = False

    async def complete(self, *a, **kw):  # pragma: no cover - never invoked
        raise AssertionError("provider.complete() should not be called by seam tests")

    async def count_tokens(self, messages, system=None, tools=None):
        return 0

    def rough_token_count(self, text):
        return len(text) // 4


def _make_loop(tools: ToolRegistry, tmp_path, on_tool_call=None) -> AgentLoop:
    provider = _NoopProvider()
    ctx = ContextManager(str(tmp_path), provider)
    return AgentLoop(
        provider=provider,
        tools=tools,
        context_manager=ctx,
        max_turns=5,
        workspace=str(tmp_path),
        on_tool_call=on_tool_call,
    )


def _tc(name: str, args: dict | None = None, tc_id: str | None = None) -> ToolCall:
    return ToolCall(id=tc_id or f"tu_{name}", name=name, arguments=args or {})


class TestProcessToolCallsSeam:
    @pytest.mark.asyncio
    async def test_executes_each_call_and_appends_tool_messages(self, tmp_path):
        tools = ToolRegistry()
        tools.register(_RecordingTool("file_read", output="contents"))
        tools.register(_RecordingTool("grep", output="matches"))
        loop = _make_loop(tools, tmp_path)

        messages: list[Message] = []
        api_messages: list[Message] = []
        ctx = ToolContext(workspace=str(tmp_path))

        outcome = await loop._process_tool_calls(
            turn=0,
            tool_calls=[_tc("file_read", {"file_path": "a.txt"}), _tc("grep", {"pattern": "x"})],
            messages=messages,
            api_messages=api_messages,
            tool_context=ctx,
            tool_cache=ToolCache(),
            ws_state=WorkspaceState(),
        )

        assert outcome.count == 2
        assert len(messages) == 2
        assert len(api_messages) == 2
        for m in messages:
            assert m.role == "tool"
            assert m.tool_call_id is not None
            assert m.tool_name in {"file_read", "grep"}

    @pytest.mark.asyncio
    async def test_cache_miss_executes_then_puts(self, tmp_path):
        tools = ToolRegistry()
        grep = _RecordingTool("grep", output="matches")
        tools.register(grep)
        loop = _make_loop(tools, tmp_path)

        cache = ToolCache()
        ctx = ToolContext(workspace=str(tmp_path))

        await loop._process_tool_calls(
            turn=0,
            tool_calls=[_tc("grep", {"pattern": "x"})],
            messages=[],
            api_messages=[],
            tool_context=ctx,
            tool_cache=cache,
            ws_state=WorkspaceState(),
        )

        assert len(grep.calls) == 1
        assert cache.get("grep", {"pattern": "x"}) is not None

    @pytest.mark.asyncio
    async def test_cache_hit_skips_execute(self, tmp_path):
        tools = ToolRegistry()
        grep = _RecordingTool("grep", output="cold")
        tools.register(grep)
        loop = _make_loop(tools, tmp_path)

        cache = ToolCache()
        cache.put("grep", {"pattern": "x"}, ToolResult(output="cached"))
        ctx = ToolContext(workspace=str(tmp_path))

        messages: list[Message] = []
        api_messages: list[Message] = []
        outcome = await loop._process_tool_calls(
            turn=0,
            tool_calls=[_tc("grep", {"pattern": "x"})],
            messages=messages,
            api_messages=api_messages,
            tool_context=ctx,
            tool_cache=cache,
            ws_state=WorkspaceState(),
        )

        assert grep.calls == []  # tool not executed
        assert outcome.count == 1
        assert messages[0].content == "cached"

    @pytest.mark.asyncio
    async def test_write_tool_invalidates_cache(self, tmp_path):
        tools = ToolRegistry()
        tools.register(_RecordingTool("file_write", output="written"))
        loop = _make_loop(tools, tmp_path)

        cache = ToolCache()
        cache.put("grep", {"pattern": "x"}, ToolResult(output="cached"))
        assert cache.size == 1

        await loop._process_tool_calls(
            turn=0,
            tool_calls=[_tc("file_write", {"file_path": "a.txt", "content": "hi"})],
            messages=[],
            api_messages=[],
            tool_context=ToolContext(workspace=str(tmp_path)),
            tool_cache=cache,
            ws_state=WorkspaceState(),
        )

        assert cache.size == 0

    @pytest.mark.asyncio
    async def test_file_write_sets_wrote_flag(self, tmp_path):
        tools = ToolRegistry()
        tools.register(_RecordingTool("file_write"))
        loop = _make_loop(tools, tmp_path)

        outcome = await loop._process_tool_calls(
            turn=0,
            tool_calls=[_tc("file_write", {"file_path": "x", "content": "y"})],
            messages=[],
            api_messages=[],
            tool_context=ToolContext(workspace=str(tmp_path)),
            tool_cache=ToolCache(),
            ws_state=WorkspaceState(),
        )

        assert outcome.wrote is True
        assert outcome.verified is False
        assert outcome.turn_has_write is True

    @pytest.mark.asyncio
    async def test_file_edit_sets_wrote_flag(self, tmp_path):
        tools = ToolRegistry()
        tools.register(_RecordingTool("file_edit"))
        loop = _make_loop(tools, tmp_path)

        outcome = await loop._process_tool_calls(
            turn=0,
            tool_calls=[_tc("file_edit", {"file_path": "x"})],
            messages=[],
            api_messages=[],
            tool_context=ToolContext(workspace=str(tmp_path)),
            tool_cache=ToolCache(),
            ws_state=WorkspaceState(),
        )

        assert outcome.wrote is True

    @pytest.mark.asyncio
    async def test_test_runner_sets_verified_flag(self, tmp_path):
        tools = ToolRegistry()
        tools.register(_RecordingTool("test_runner"))
        loop = _make_loop(tools, tmp_path)

        outcome = await loop._process_tool_calls(
            turn=0,
            tool_calls=[_tc("test_runner", {})],
            messages=[],
            api_messages=[],
            tool_context=ToolContext(workspace=str(tmp_path)),
            tool_cache=ToolCache(),
            ws_state=WorkspaceState(),
        )

        assert outcome.verified is True
        assert outcome.wrote is False

    @pytest.mark.asyncio
    async def test_bash_pytest_sets_verified_flag(self, tmp_path):
        tools = ToolRegistry()
        tools.register(_RecordingTool("bash"))
        loop = _make_loop(tools, tmp_path)

        outcome = await loop._process_tool_calls(
            turn=0,
            tool_calls=[_tc("bash", {"command": "python -m pytest tests/"})],
            messages=[],
            api_messages=[],
            tool_context=ToolContext(workspace=str(tmp_path)),
            tool_cache=ToolCache(),
            ws_state=WorkspaceState(),
        )
        assert outcome.verified is True

    @pytest.mark.asyncio
    async def test_bash_non_test_does_not_verify(self, tmp_path):
        tools = ToolRegistry()
        tools.register(_RecordingTool("bash"))
        loop = _make_loop(tools, tmp_path)

        outcome = await loop._process_tool_calls(
            turn=0,
            tool_calls=[_tc("bash", {"command": "ls -la"})],
            messages=[],
            api_messages=[],
            tool_context=ToolContext(workspace=str(tmp_path)),
            tool_cache=ToolCache(),
            ws_state=WorkspaceState(),
        )
        assert outcome.verified is False

    @pytest.mark.asyncio
    async def test_turn_has_write_for_bash(self, tmp_path):
        tools = ToolRegistry()
        tools.register(_RecordingTool("bash"))
        loop = _make_loop(tools, tmp_path)

        outcome = await loop._process_tool_calls(
            turn=0,
            tool_calls=[_tc("bash", {"command": "echo hi"})],
            messages=[],
            api_messages=[],
            tool_context=ToolContext(workspace=str(tmp_path)),
            tool_cache=ToolCache(),
            ws_state=WorkspaceState(),
        )
        assert outcome.turn_has_write is True
        # bash echo is not a write to a file, so wrote stays False
        assert outcome.wrote is False

    @pytest.mark.asyncio
    async def test_turn_has_write_false_for_pure_reads(self, tmp_path):
        tools = ToolRegistry()
        tools.register(_RecordingTool("file_read"))
        tools.register(_RecordingTool("grep"))
        loop = _make_loop(tools, tmp_path)

        outcome = await loop._process_tool_calls(
            turn=0,
            tool_calls=[
                _tc("file_read", {"file_path": "a"}),
                _tc("grep", {"pattern": "x"}),
            ],
            messages=[],
            api_messages=[],
            tool_context=ToolContext(workspace=str(tmp_path)),
            tool_cache=ToolCache(),
            ws_state=WorkspaceState(),
        )
        assert outcome.turn_has_write is False

    @pytest.mark.asyncio
    async def test_streams_when_enabled(self, tmp_path):
        on_tool_call = AsyncMock()
        tools = ToolRegistry()
        tools.register(_RecordingTool("file_read", output="hello"))
        loop = _make_loop(tools, tmp_path, on_tool_call=on_tool_call)

        await loop._process_tool_calls(
            turn=3,
            tool_calls=[_tc("file_read", {"file_path": "a.txt"})],
            messages=[],
            api_messages=[],
            tool_context=ToolContext(workspace=str(tmp_path)),
            tool_cache=ToolCache(),
            ws_state=WorkspaceState(),
        )

        assert on_tool_call.await_count == 1
        args, _ = on_tool_call.await_args
        assert args[0] == "file_read"
        assert args[1] == {"file_path": "a.txt"}
        assert args[2] == "hello"
        assert args[3] == 3

    @pytest.mark.asyncio
    async def test_does_not_stream_when_no_callback_configured(self, tmp_path):
        # Natural gate: when on_tool_call isn't wired, the seam never tries to stream.
        tools = ToolRegistry()
        tools.register(_RecordingTool("file_read"))
        loop = _make_loop(tools, tmp_path, on_tool_call=None)

        outcome = await loop._process_tool_calls(
            turn=0,
            tool_calls=[_tc("file_read", {"file_path": "a"})],
            messages=[],
            api_messages=[],
            tool_context=ToolContext(workspace=str(tmp_path)),
            tool_cache=ToolCache(),
            ws_state=WorkspaceState(),
        )

        # No callback set → bookkeeping still runs, just no streaming.
        assert outcome.count == 1

    @pytest.mark.asyncio
    async def test_stream_callback_exception_swallowed(self, tmp_path):
        async def boom(*a, **kw):
            raise RuntimeError("ui crashed")

        tools = ToolRegistry()
        tools.register(_RecordingTool("file_read"))
        loop = _make_loop(tools, tmp_path, on_tool_call=boom)

        messages: list[Message] = []
        outcome = await loop._process_tool_calls(
            turn=0,
            tool_calls=[_tc("file_read", {"file_path": "a"})],
            messages=messages,
            api_messages=[],
            tool_context=ToolContext(workspace=str(tmp_path)),
            tool_cache=ToolCache(),
            ws_state=WorkspaceState(),
        )

        # No exception bubbled up; bookkeeping still complete
        assert outcome.count == 1
        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_ws_state_called_per_tool(self, tmp_path):
        tools = ToolRegistry()
        tools.register(_RecordingTool("file_read"))
        tools.register(_RecordingTool("file_write"))
        loop = _make_loop(tools, tmp_path)

        ws = WorkspaceState()
        await loop._process_tool_calls(
            turn=0,
            tool_calls=[
                _tc("file_read", {"file_path": "a.txt"}),
                _tc("file_write", {"file_path": "b.txt", "content": "x"}),
            ],
            messages=[],
            api_messages=[],
            tool_context=ToolContext(workspace=str(tmp_path)),
            tool_cache=ToolCache(),
            ws_state=ws,
        )

        assert "a.txt" in ws.files
        assert "b.txt" in ws.files
        assert ws.files["b.txt"].was_modified is True
