"""Tests for the LLM tool-loop primitive `converse`."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.llm.types import LLMResponse, Message, ToolCall
from agent.slack_assistant import converse

pytestmark = pytest.mark.asyncio


def _resp(content: str = "", tool_calls: list[ToolCall] | None = None,
          stop_reason: str = "end_turn") -> LLMResponse:
    return LLMResponse(
        message=Message(role="assistant", content=content, tool_calls=tool_calls or []),
        stop_reason=stop_reason,
        usage=None,
    )


async def test_converse_returns_appended_messages_and_reply():
    history: list[Message] = []
    fake_provider = AsyncMock()
    fake_provider.complete.return_value = _resp(content="hello!")
    with patch("agent.slack_assistant.get_provider", return_value=fake_provider), \
         patch("agent.slack_assistant.resolve_home_dir", return_value=None):
        reply, appended = await converse(
            user_id=1, text="hi", history=history,
            home_dir=None, on_create_task=None,
        )
    assert reply == "hello!"
    # appended = the user msg + the assistant reply (2 entries)
    assert len(appended) == 2
    assert appended[0].role == "user"
    assert appended[1].role == "assistant"


async def test_converse_invokes_on_create_task_when_create_task_tool_fires():
    history: list[Message] = []
    fake_provider = AsyncMock()
    fake_provider.complete.side_effect = [
        _resp(
            tool_calls=[ToolCall(id="t1", name="create_task", arguments={
                "repo_name": "cardamon", "description": "test task",
            })],
            stop_reason="tool_use",
        ),
        _resp(content="created!"),
    ]
    on_create_task = AsyncMock()
    with patch("agent.slack_assistant.get_provider", return_value=fake_provider), \
         patch("agent.slack_assistant.resolve_home_dir", return_value=None), \
         patch("agent.slack_assistant._create_task",
               AsyncMock(return_value={"task_id": 77, "status": "queued", "title": "x"})):
        reply, _ = await converse(
            user_id=1, text="create a test task on cardamon",
            history=history, home_dir=None, on_create_task=on_create_task,
        )
    assert reply == "created!"
    on_create_task.assert_awaited_once_with(77)
