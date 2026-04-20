"""Tests for the critical tool_result batching in LLM providers.

Anthropic's API requires all tool_results responding to one assistant turn's
tool_uses to be in a SINGLE user message. Splitting them into separate user
messages breaks the conversation — the assistant re-calls the same tools.

This was the bug causing the "Groundhog Day" read loop on multi-file tasks.
"""

from agent.llm.bedrock import BedrockProvider
from agent.llm.anthropic import AnthropicProvider
from agent.llm.types import Message, ToolCall


def _make_bedrock_provider() -> BedrockProvider:
    return BedrockProvider(region="us-east-1", model="claude-sonnet-4-6")


def _make_anthropic_provider() -> AnthropicProvider:
    return AnthropicProvider(api_key="dummy-key", model="claude-sonnet-4-6")


class TestBedrockBatching:
    def test_multiple_tool_results_batched_into_one_user_message(self):
        """The critical invariant: 4 tool_results → 1 user message, not 4."""
        provider = _make_bedrock_provider()

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

        api_messages = provider._build_api_messages(messages)

        # Expected: user (task), assistant (tool_uses), user (4 tool_results)
        assert len(api_messages) == 3, f"Expected 3 messages, got {len(api_messages)}"

        # First: original user task
        assert api_messages[0]["role"] == "user"
        assert api_messages[0]["content"] == "Read my files"

        # Second: assistant with text + 4 tool_uses
        assert api_messages[1]["role"] == "assistant"
        content = api_messages[1]["content"]
        assert isinstance(content, list)
        tool_uses = [b for b in content if b["type"] == "tool_use"]
        assert len(tool_uses) == 4

        # THIRD (the critical one): ONE user message containing ALL 4 tool_results
        assert api_messages[2]["role"] == "user"
        content = api_messages[2]["content"]
        assert isinstance(content, list), "tool_results must be in a content array"
        tool_results = [b for b in content if b["type"] == "tool_result"]
        assert len(tool_results) == 4, (
            f"All 4 tool_results must be in one user message, got {len(tool_results)}. "
            "Splitting them causes the API to lose the tool_use -> tool_result association."
        )

        # Verify all IDs present
        ids_present = {tr["tool_use_id"] for tr in tool_results}
        assert ids_present == {"a", "b", "c", "d"}

    def test_single_tool_result_still_works(self):
        """Single tool calls still produce valid structure."""
        provider = _make_bedrock_provider()

        messages = [
            Message(role="user", content="do it"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="x", name="grep", arguments={"pattern": "foo"})],
            ),
            Message(role="tool", content="matches", tool_call_id="x", tool_name="grep"),
        ]

        api = provider._build_api_messages(messages)
        assert len(api) == 3
        assert api[2]["role"] == "user"
        assert len(api[2]["content"]) == 1
        assert api[2]["content"][0]["type"] == "tool_result"

    def test_multi_turn_conversation_batches_correctly(self):
        """Multiple assistant turns each get their own grouped tool_results."""
        provider = _make_bedrock_provider()

        messages = [
            Message(role="user", content="start"),
            # Turn 1: 2 tool calls
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
            # Turn 2: 1 tool call
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="t2a", name="file_write", arguments={"file_path": "c.py"}),
                ],
            ),
            Message(role="tool", content="ok", tool_call_id="t2a", tool_name="file_write"),
        ]

        api = provider._build_api_messages(messages)
        # user, assistant, user (2 results), assistant, user (1 result)
        assert len(api) == 5
        assert len(api[2]["content"]) == 2  # Turn 1 tool results batched
        assert len(api[4]["content"]) == 1  # Turn 2 single tool result

    def test_text_user_message_flushes_pending_results(self):
        """If a text user message appears between tool and next assistant,
        pending tool_results must flush first."""
        provider = _make_bedrock_provider()

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

        api = provider._build_api_messages(messages)
        # user, assistant, user(tool_result), user(text)
        assert len(api) == 4
        assert api[2]["content"][0]["type"] == "tool_result"
        assert api[3]["content"] == "keep going"

    def test_system_messages_skipped(self):
        """System-role messages must not appear in api_messages (they go in the system param)."""
        provider = _make_bedrock_provider()

        messages = [
            Message(role="system", content="be helpful"),
            Message(role="user", content="hi"),
        ]
        api = provider._build_api_messages(messages)
        assert len(api) == 1
        assert api[0]["role"] == "user"


class TestAnthropicBatching:
    """Same invariants apply to the non-Bedrock Anthropic provider."""

    def test_multiple_tool_results_batched(self):
        provider = _make_anthropic_provider()

        messages = [
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

        api = provider._build_api_messages(messages)
        assert len(api) == 3
        tool_results = [b for b in api[2]["content"] if b["type"] == "tool_result"]
        assert len(tool_results) == 2
