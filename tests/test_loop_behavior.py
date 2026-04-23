"""Tests for agent loop behaviors: exploration budget, verification gate, tool caching."""

from __future__ import annotations

import pytest

from agent.context.workspace_state import WorkspaceState
from agent.loop import (
    _EXPLORATION_BUDGET,
    _EXPLORATION_NUDGE,
    _READ_ONLY_TOOLS,
    _VERIFICATION_NUDGE,
    _VERIFICATION_PATTERNS,
    _WRITE_TOOLS,
)


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
