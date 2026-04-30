from unittest.mock import MagicMock, patch

import pytest

from agent.llm.types import Message, TokenUsage
from agent.search_loop import run_search_turn


class _FakeAgentLoop:
    """Stub AgentLoop that drives the on_thinking / on_tool_call callbacks
    and then returns an AgentResult with a final answer."""

    def __init__(self, *args, **kwargs):
        self._on_thinking = kwargs.get("on_thinking")
        self._on_tool_call = kwargs.get("on_tool_call")

    async def run(self, prompt, system=None, resume=False):
        if self._on_tool_call:
            await self._on_tool_call("web_search", {"query": "alpha"}, "ok", 0)
        if self._on_thinking:
            await self._on_thinking("Hello", 0)
            await self._on_thinking(" world.", 0)
        from agent.loop import AgentResult
        return AgentResult(
            output="Hello world.",
            tool_calls_made=1,
            tokens_used=TokenUsage(),
            messages=[Message(role="assistant", content="Hello world.")],
        )


@pytest.mark.asyncio
async def test_run_search_turn_emits_expected_events():
    history = [{"role": "user", "content": "What is alpha?"}]
    events: list[dict] = []
    with patch("agent.search_loop.AgentLoop", _FakeAgentLoop), \
         patch("agent.search_loop.get_provider", return_value=MagicMock(is_passthrough=False)):
        async for ev in run_search_turn(
            user_message="What is alpha?",
            history=history,
            brave_api_key="fake",
            author="alan",
        ):
            events.append(ev)

    types = [e["type"] for e in events]
    assert "tool_call_start" in types
    assert "text" in types
    assert types[-1] == "done"
    text_events = [e for e in events if e["type"] == "text"]
    assert "".join(e["delta"] for e in text_events) == "Hello world."


@pytest.mark.asyncio
async def test_run_search_turn_emits_error_on_exception():
    class _Boom:
        def __init__(self, *a, **kw): pass
        async def run(self, *a, **kw): raise RuntimeError("boom")

    events: list[dict] = []
    with patch("agent.search_loop.AgentLoop", _Boom), \
         patch("agent.search_loop.get_provider", return_value=MagicMock(is_passthrough=False)):
        async for ev in run_search_turn(
            user_message="x", history=[], brave_api_key="fake", author=None,
        ):
            events.append(ev)
    assert events[-1]["type"] == "error"
    assert "boom" in events[-1]["message"]
