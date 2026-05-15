"""Test the ``message_from_dict`` rehydrator.

Production repro 2026-05-15: the messenger router persists Message
dataclasses to ``messenger_conversations.messages_json`` via
``dataclasses.asdict(m)``. On the next turn it reconstructs them with
``Message(**m)`` — but that splat leaves ``tool_calls`` as a
``list[dict]`` (Python dataclasses don't coerce). The Bedrock provider's
``to_api_messages`` then crashes with
``AttributeError: 'dict' object has no attribute 'id'`` because it
expects ``ToolCall`` instances. The fix is a proper rehydrator that
reconstructs ``ToolCall`` objects from their dict form.
"""

from __future__ import annotations

import dataclasses

from agent.llm.types import Message, ToolCall, message_from_dict


def test_rehydrates_tool_calls_to_dataclass_instances():
    serialized = {
        "role": "assistant",
        "content": "checking…",
        "tool_calls": [
            {"id": "t1", "name": "list_my_tasks", "arguments": {"status": "active"}},
            {"id": "t2", "name": "get_task", "arguments": {"task_id": 5}},
        ],
        "tool_call_id": None,
        "tool_name": None,
        "token_estimate": None,
    }
    m = message_from_dict(serialized)
    assert m.role == "assistant"
    assert m.tool_calls is not None
    assert all(isinstance(tc, ToolCall) for tc in m.tool_calls), (
        f"expected ToolCall instances, got {[type(tc) for tc in m.tool_calls]}"
    )
    assert m.tool_calls[0].id == "t1"
    assert m.tool_calls[0].name == "list_my_tasks"
    assert m.tool_calls[0].arguments == {"status": "active"}


def test_no_tool_calls_stays_none():
    m = message_from_dict({"role": "user", "content": "hi"})
    assert m.role == "user"
    assert m.content == "hi"
    assert m.tool_calls is None


def test_asdict_roundtrip_preserves_tool_calls():
    original = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="t1", name="approve_plan", arguments={"task_id": 5})],
    )
    rehydrated = message_from_dict(dataclasses.asdict(original))
    assert rehydrated == original
