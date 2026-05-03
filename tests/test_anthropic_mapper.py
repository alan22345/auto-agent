"""Tests for the Anthropic message mapper.

The mapper owns Message <-> Anthropic-API translation for both BedrockProvider
and AnthropicProvider. The "tool_results batched into one user message"
invariant — load-bearing per CLAUDE.md and previously the source of the
"Groundhog Day" read-loop bug — lives here. These tests are the authoritative
spec for that invariant.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.llm.anthropic_mapper import (
    from_api_response,
    to_api_messages,
    to_api_tool,
)
from agent.llm.types import Message, ToolCall, ToolDefinition

# ---------------------------------------------------------------------------
# to_api_messages — batching invariant
# ---------------------------------------------------------------------------


class TestToApiMessages:
    def test_multiple_tool_results_batched_into_one_user_message(self):
        """The critical invariant: 4 tool_results -> 1 user message, not 4."""
        messages = [
            Message(role="user", content="Read my files"),
            Message(
                role="assistant",
                content="Reading files...",
                tool_calls=[
                    ToolCall(id="a", name="file_read", arguments={"file_path": "app.py"}),
                    ToolCall(id="b", name="file_read", arguments={"file_path": "models.py"}),
                    ToolCall(id="c", name="file_read", arguments={"file_path": "routes.py"}),
                    ToolCall(id="d", name="file_read", arguments={"file_path": "tests.py"}),
                ],
            ),
            Message(role="tool", content="app content", tool_call_id="a", tool_name="file_read"),
            Message(role="tool", content="models content", tool_call_id="b", tool_name="file_read"),
            Message(role="tool", content="routes content", tool_call_id="c", tool_name="file_read"),
            Message(role="tool", content="tests content", tool_call_id="d", tool_name="file_read"),
        ]

        api = to_api_messages(messages)

        assert len(api) == 3, f"Expected 3 messages, got {len(api)}"

        assert api[0]["role"] == "user"
        assert api[0]["content"] == "Read my files"

        assert api[1]["role"] == "assistant"
        assistant_content = api[1]["content"]
        assert isinstance(assistant_content, list)
        tool_uses = [b for b in assistant_content if b["type"] == "tool_use"]
        assert len(tool_uses) == 4

        assert api[2]["role"] == "user"
        results_content = api[2]["content"]
        assert isinstance(results_content, list), "tool_results must be in a content array"
        tool_results = [b for b in results_content if b["type"] == "tool_result"]
        assert len(tool_results) == 4, (
            f"All 4 tool_results must be in one user message, got {len(tool_results)}. "
            "Splitting them causes the API to lose the tool_use -> tool_result association."
        )

        assert {tr["tool_use_id"] for tr in tool_results} == {"a", "b", "c", "d"}

    def test_single_tool_result_still_works(self):
        messages = [
            Message(role="user", content="do it"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="x", name="grep", arguments={"pattern": "foo"})],
            ),
            Message(role="tool", content="matches", tool_call_id="x", tool_name="grep"),
        ]

        api = to_api_messages(messages)
        assert len(api) == 3
        assert api[2]["role"] == "user"
        assert len(api[2]["content"]) == 1
        assert api[2]["content"][0]["type"] == "tool_result"
        assert api[2]["content"][0]["tool_use_id"] == "x"
        assert api[2]["content"][0]["content"] == "matches"

    def test_multi_turn_conversation_batches_each_turn_separately(self):
        """Each assistant turn's tool_results group into its own user message."""
        messages = [
            Message(role="user", content="start"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="t1a", name="file_read", arguments={"file_path": "a.py"}),
                    ToolCall(id="t1b", name="file_read", arguments={"file_path": "b.py"}),
                ],
            ),
            Message(role="tool", content="a", tool_call_id="t1a", tool_name="file_read"),
            Message(role="tool", content="b", tool_call_id="t1b", tool_name="file_read"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="t2a", name="file_write", arguments={"file_path": "c.py"}),
                ],
            ),
            Message(role="tool", content="ok", tool_call_id="t2a", tool_name="file_write"),
        ]

        api = to_api_messages(messages)
        # user, assistant, user(2 results), assistant, user(1 result)
        assert len(api) == 5
        assert len(api[2]["content"]) == 2
        assert len(api[4]["content"]) == 1

    def test_text_user_message_flushes_pending_results(self):
        """A text user message between tool and next assistant must flush
        pending tool_results first — otherwise the API rejects the request
        with a tool_use_id mismatch."""
        messages = [
            Message(role="user", content="start"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="x", name="grep", arguments={"pattern": "y"})],
            ),
            Message(role="tool", content="result", tool_call_id="x", tool_name="grep"),
            Message(role="user", content="keep going"),
        ]

        api = to_api_messages(messages)
        assert len(api) == 4
        assert api[2]["content"][0]["type"] == "tool_result"
        assert api[3]["content"] == "keep going"

    def test_system_messages_skipped(self):
        """System messages are passed via the API's `system` param, not in messages."""
        messages = [
            Message(role="system", content="be helpful"),
            Message(role="user", content="hi"),
        ]
        api = to_api_messages(messages)
        assert len(api) == 1
        assert api[0]["role"] == "user"
        assert api[0]["content"] == "hi"

    def test_assistant_with_text_and_tool_calls(self):
        """Assistant turn with both text and tool_calls emits a content array
        with text block first, then tool_use blocks."""
        messages = [
            Message(
                role="assistant",
                content="thinking out loud",
                tool_calls=[ToolCall(id="z", name="bash", arguments={"command": "ls"})],
            ),
        ]
        api = to_api_messages(messages)
        assert len(api) == 1
        content = api[0]["content"]
        assert content[0] == {"type": "text", "text": "thinking out loud"}
        assert content[1]["type"] == "tool_use"
        assert content[1]["id"] == "z"
        assert content[1]["name"] == "bash"
        assert content[1]["input"] == {"command": "ls"}

    def test_assistant_text_only(self):
        messages = [
            Message(role="user", content="hi"),
            Message(role="assistant", content="hello back"),
        ]
        api = to_api_messages(messages)
        assert api[1] == {"role": "assistant", "content": "hello back"}

    def test_trailing_tool_results_still_flushed(self):
        """Tool messages at the end of the list (no following non-tool message)
        must still be emitted, not silently dropped."""
        messages = [
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="x", name="g", arguments={})],
            ),
            Message(role="tool", content="r", tool_call_id="x", tool_name="g"),
        ]
        api = to_api_messages(messages)
        assert len(api) == 2
        assert api[1]["role"] == "user"
        assert api[1]["content"][0]["type"] == "tool_result"


# ---------------------------------------------------------------------------
# to_api_tool — schema fixup
# ---------------------------------------------------------------------------


class TestToApiTool:
    def test_schema_with_type_object_unchanged(self):
        tool = ToolDefinition(
            name="grep",
            description="search",
            parameters={"type": "object", "properties": {"pattern": {"type": "string"}}},
        )
        api = to_api_tool(tool)
        assert api["name"] == "grep"
        assert api["description"] == "search"
        assert api["input_schema"]["type"] == "object"
        assert api["input_schema"]["properties"] == {"pattern": {"type": "string"}}

    def test_schema_missing_type_gets_object_injected(self):
        """Bedrock requires explicit "type": "object" on tool input schemas;
        injecting it is also valid for native Anthropic. Always-on."""
        tool = ToolDefinition(
            name="bash",
            description="run a shell command",
            parameters={"properties": {"command": {"type": "string"}}},
        )
        api = to_api_tool(tool)
        assert api["input_schema"]["type"] == "object"
        assert api["input_schema"]["properties"] == {"command": {"type": "string"}}

    def test_does_not_mutate_input_schema(self):
        """Mapper must return a fresh dict — mutating the caller's schema
        would surprise other consumers (the tool registry reuses parameters)."""
        original = {"properties": {"x": {"type": "string"}}}
        tool = ToolDefinition(name="t", description="d", parameters=original)
        to_api_tool(tool)
        assert "type" not in original

    def test_non_dict_schema_passes_through(self):
        """Defensive: if a tool somehow ships a non-dict schema, don't crash."""
        tool = ToolDefinition(name="t", description="d", parameters={})
        api = to_api_tool(tool)
        # An empty dict is missing "type", so the mapper injects it.
        assert api["input_schema"] == {"type": "object"}


# ---------------------------------------------------------------------------
# from_api_response — wire response -> LLMResponse
# ---------------------------------------------------------------------------


def _block(type_: str, **kwargs) -> SimpleNamespace:
    return SimpleNamespace(type=type_, **kwargs)


def _fake_response(blocks, stop_reason="end_turn", input_tokens=10, output_tokens=5):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class TestFromApiResponse:
    def test_text_only_response(self):
        resp = _fake_response([_block("text", text="hello")])
        out = from_api_response(resp)
        assert out.message.role == "assistant"
        assert out.message.content == "hello"
        assert out.message.tool_calls is None
        assert out.stop_reason == "end_turn"
        assert out.usage.input_tokens == 10
        assert out.usage.output_tokens == 5

    def test_tool_use_only_response(self):
        resp = _fake_response(
            [_block("tool_use", id="t1", name="grep", input={"pattern": "foo"})],
            stop_reason="tool_use",
        )
        out = from_api_response(resp)
        assert out.message.content == ""
        assert out.message.tool_calls is not None
        assert len(out.message.tool_calls) == 1
        tc = out.message.tool_calls[0]
        assert tc.id == "t1"
        assert tc.name == "grep"
        assert tc.arguments == {"pattern": "foo"}
        assert out.stop_reason == "tool_use"

    def test_mixed_text_and_tool_use(self):
        resp = _fake_response(
            [
                _block("text", text="let me check"),
                _block("tool_use", id="t1", name="grep", input={"pattern": "x"}),
                _block("text", text="...running"),
            ],
            stop_reason="tool_use",
        )
        out = from_api_response(resp)
        # Multiple text parts joined with newline (preserves existing behavior)
        assert "let me check" in out.message.content
        assert "...running" in out.message.content
        assert out.message.tool_calls is not None
        assert len(out.message.tool_calls) == 1

    def test_tool_use_with_json_string_input(self):
        """The Anthropic SDK occasionally returns input as a JSON string
        instead of a parsed dict. The mapper must json.loads it."""
        resp = _fake_response(
            [_block("tool_use", id="t1", name="bash", input='{"command": "ls -la"}')],
            stop_reason="tool_use",
        )
        out = from_api_response(resp)
        assert out.message.tool_calls[0].arguments == {"command": "ls -la"}

    def test_max_tokens_stop_reason(self):
        resp = _fake_response([_block("text", text="truncated")], stop_reason="max_tokens")
        out = from_api_response(resp)
        assert out.stop_reason == "max_tokens"

    def test_unknown_stop_reason_defaults_to_end_turn(self):
        resp = _fake_response([_block("text", text="x")], stop_reason="something_new")
        out = from_api_response(resp)
        assert out.stop_reason == "end_turn"

    def test_usage_is_propagated(self):
        resp = _fake_response([_block("text", text="ok")], input_tokens=1234, output_tokens=56)
        out = from_api_response(resp)
        assert out.usage.input_tokens == 1234
        assert out.usage.output_tokens == 56
        assert out.usage.total == 1234 + 56


# ---------------------------------------------------------------------------
# Round-trip sanity: types stay model-agnostic
# ---------------------------------------------------------------------------


def test_mapper_has_no_async_callable():
    """The mapper is pure data translation — no awaitables should escape."""
    import inspect

    import agent.llm.anthropic_mapper as m

    for name, obj in vars(m).items():
        if name.startswith("_"):
            continue
        if inspect.iscoroutinefunction(obj):
            pytest.fail(f"{name} is async; mapper must be pure synchronous data translation")
