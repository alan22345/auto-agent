"""Regression test for the 400 error seen on task 51.

Scenario: Anthropic returns stop_reason="max_tokens" with a fully-formed
tool_use block in the message. Our loop was appending the assistant
message and then a user "continuation" nudge — without executing the
tool_use first. The next API call then had an orphan tool_use followed
by a user text message, which Bedrock rejects with:

    messages.N: `tool_use` ids were found without `tool_result` blocks
    immediately after

The fix: when stop_reason is "max_tokens" AND the assistant emitted
tool_calls, execute them (producing matched tool_results) before
injecting the continuation nudge.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.context import ContextManager
from agent.llm.types import LLMResponse, Message, TokenUsage, ToolCall
from agent.loop import AgentLoop
from agent.tools.base import Tool, ToolContext, ToolRegistry, ToolResult


class _StubTool(Tool):
    name = "stub"
    description = "test tool"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, arguments, context):
        return ToolResult(output="stub-result")


class _StubProvider:
    """Returns a canned sequence of responses."""

    model = "claude-sonnet-4-6"
    max_context_tokens = 200_000
    is_passthrough = False

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[list[Message]] = []  # Record messages sent on each call

    async def complete(self, messages, tools=None, system=None, max_tokens=8192, temperature=0.0):
        # Snapshot the messages we were asked to send
        self.calls.append(list(messages))
        if not self._responses:
            raise RuntimeError("Stub provider ran out of responses")
        return self._responses.pop(0)

    async def count_tokens(self, messages, system=None, tools=None):
        return sum(len(m.content or "") // 4 for m in messages)

    def rough_token_count(self, text):
        return len(text) // 4


def _max_tokens_with_tool_call() -> LLMResponse:
    """Assistant response that hit max_tokens while emitting a complete tool_use."""
    return LLMResponse(
        message=Message(
            role="assistant",
            content="I need to search for something",
            tool_calls=[ToolCall(id="tu_ABC", name="stub", arguments={})],
        ),
        stop_reason="max_tokens",
        usage=TokenUsage(input_tokens=100, output_tokens=8192),
    )


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        message=Message(role="assistant", content=text),
        stop_reason="end_turn",
        usage=TokenUsage(input_tokens=50, output_tokens=10),
    )


class TestMaxTokensWithToolUse:
    @pytest.mark.asyncio
    async def test_tool_use_is_executed_on_max_tokens(self, tmp_path):
        """Pin the fix: a max_tokens response with a valid tool_use must have
        that tool executed so the next LLM call isn't looking at an orphan."""
        provider = _StubProvider([
            _max_tokens_with_tool_call(),  # Turn 1: max_tokens + tool_use
            _end_turn("finished"),           # Turn 2: model wraps up
        ])
        tools = ToolRegistry()
        tools.register(_StubTool())
        ctx = ContextManager(str(tmp_path), provider)

        agent = AgentLoop(
            provider=provider, tools=tools, context_manager=ctx,
            max_turns=5, workspace=str(tmp_path),
        )
        result = await agent.run("please do work", system="test")

        # The bug repro: turn 2's messages must NOT contain an orphan tool_use.
        assert len(provider.calls) == 2, f"Expected 2 LLM calls, got {len(provider.calls)}"
        turn_2_msgs = provider.calls[1]

        # Walk turn_2 and verify every tool_use has a matching tool_result RIGHT AFTER
        for i, msg in enumerate(turn_2_msgs):
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    # Look forward for a tool_result with matching id
                    found = False
                    for j in range(i + 1, len(turn_2_msgs)):
                        later = turn_2_msgs[j]
                        if later.role == "tool" and later.tool_call_id == tc.id:
                            found = True
                            break
                        # If we hit a non-tool message before finding the match, orphan.
                        if later.role != "tool":
                            break
                    assert found, (
                        f"Orphan tool_use detected: id={tc.id} in assistant message at index {i} "
                        f"has no matching tool_result message following it. "
                        f"This is the task-51 failure mode."
                    )

    @pytest.mark.asyncio
    async def test_max_tokens_without_tool_call_still_works(self, tmp_path):
        """The plain max_tokens → continuation path (no tool_calls) must keep working."""
        provider = _StubProvider([
            LLMResponse(
                message=Message(role="assistant", content="... interrupted mid-sentence"),
                stop_reason="max_tokens",
                usage=TokenUsage(input_tokens=10, output_tokens=8192),
            ),
            _end_turn("continued and finished"),
        ])
        tools = ToolRegistry()
        ctx = ContextManager(str(tmp_path), provider)
        agent = AgentLoop(
            provider=provider, tools=tools, context_manager=ctx,
            max_turns=5, workspace=str(tmp_path),
        )
        result = await agent.run("task", system="test")
        # No assertion on structure — just no exception and the agent finishes.
        assert "continued" in result.output
