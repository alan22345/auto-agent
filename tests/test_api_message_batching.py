"""Provider-level smoke tests verifying the wire format seam is wired.

The Anthropic-format conversion logic itself is exhaustively tested in
`tests/test_anthropic_mapper.py`. The tests here only confirm that each
provider's `complete()` actually delegates to the mapper before calling its
client — i.e. that the seam exists where we expect it. Without these, a
regression that bypasses the mapper would only surface in production.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.llm.anthropic import AnthropicProvider
from agent.llm.anthropic_mapper import to_api_messages
from agent.llm.bedrock import BedrockProvider
from agent.llm.types import Message, ToolCall


def _conversation_with_batched_tool_results() -> list[Message]:
    return [
        Message(role="user", content="task"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="a", name="file_read", arguments={"file_path": "a.py"}),
                ToolCall(id="b", name="file_read", arguments={"file_path": "b.py"}),
            ],
        ),
        Message(role="tool", content="ax", tool_call_id="a", tool_name="file_read"),
        Message(role="tool", content="bx", tool_call_id="b", tool_name="file_read"),
    ]


def _stub_response() -> MagicMock:
    resp = MagicMock()
    resp.content = [MagicMock(type="text", text="ok")]
    resp.stop_reason = "end_turn"
    resp.usage = MagicMock(input_tokens=1, output_tokens=1)
    return resp


@pytest.mark.asyncio
async def test_bedrock_complete_routes_messages_through_mapper():
    provider = BedrockProvider(region="us-east-1", model="claude-sonnet-4-6")
    create = AsyncMock(return_value=_stub_response())
    provider._client.messages = MagicMock()
    provider._client.messages.create = create

    convo = _conversation_with_batched_tool_results()
    await provider.complete(messages=convo)

    create.assert_awaited_once()
    sent = create.await_args.kwargs["messages"]
    assert sent == to_api_messages(convo)


@pytest.mark.asyncio
async def test_anthropic_complete_routes_messages_through_mapper():
    provider = AnthropicProvider(api_key="dummy", model="claude-sonnet-4-6")
    create = AsyncMock(return_value=_stub_response())
    provider._client.messages = MagicMock()
    provider._client.messages.create = create

    convo = _conversation_with_batched_tool_results()
    await provider.complete(messages=convo)

    create.assert_awaited_once()
    sent = create.await_args.kwargs["messages"]
    assert sent == to_api_messages(convo)
