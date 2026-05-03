"""Anthropic Messages API <-> domain types.

Single owner of the Anthropic wire format. Both BedrockProvider and
AnthropicProvider speak this same wire format and delegate translation here,
so the providers remain thin transport+auth adapters.

Load-bearing invariant (see CLAUDE.md "critical invariants" #2):

    Anthropic's API requires all tool_results responding to one assistant
    turn's tool_uses to be grouped in ONE user message's content array — not
    split across multiple user messages. Splitting them silently breaks the
    conversation: the assistant may think its tool calls weren't answered and
    repeat them, causing the "Groundhog Day" read loop.

`tests/test_anthropic_mapper.py` is the authoritative spec — keep it green
when changing this module.
"""

from __future__ import annotations

import json
from typing import Any

from agent.llm.types import (
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolDefinition,
)

_STOP_REASONS = {"end_turn", "tool_use", "max_tokens"}


def to_api_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert domain Messages to Anthropic API `messages` payload.

    Consecutive `tool` messages are batched into a single user message whose
    content array holds one `tool_result` block per tool call. System
    messages are dropped — they belong in the API's top-level `system` param.
    """
    api_messages: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            api_messages.append(
                {
                    "role": "user",
                    "content": list(pending_tool_results),
                }
            )
            pending_tool_results.clear()

    for msg in messages:
        if msg.role == "system":
            continue

        if msg.role == "tool":
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                }
            )
            continue

        # Any non-tool message must flush pending tool_results first so the
        # tool_use -> tool_result chain stays contiguous.
        flush_tool_results()

        if msg.role == "assistant" and msg.tool_calls:
            content: list[dict[str, Any]] = []
            if msg.content:
                content.append({"type": "text", "text": msg.content})
            for tc in msg.tool_calls:
                content.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    }
                )
            api_messages.append({"role": "assistant", "content": content})
        else:
            api_messages.append({"role": msg.role, "content": msg.content})

    # Trailing tool results — uncommon but must not be dropped.
    flush_tool_results()
    return api_messages


def to_api_tool(tool: ToolDefinition) -> dict[str, Any]:
    """Convert a ToolDefinition to the Anthropic API tool payload.

    Always injects `"type": "object"` at the schema root when missing. Bedrock
    rejects schemas without it, and the JSON Schema spec requires it for an
    object-shaped input regardless. This is a no-op for the native Anthropic
    provider in practice — every tool in the registry already declares
    `"type": "object"` — but it widens the contract slightly: the mapper
    will now also accept and fix up tools that omit it, where the previous
    AnthropicProvider would have forwarded them verbatim. Idempotent.
    """
    schema = tool.parameters
    if isinstance(schema, dict) and "type" not in schema:
        schema = {**schema, "type": "object"}
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": schema,
    }


def from_api_response(response: Any) -> LLMResponse:
    """Convert an Anthropic API response object to a domain LLMResponse."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for block in response.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            arguments = block.input if isinstance(block.input, dict) else json.loads(block.input)
            tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=arguments))

    stop_reason = response.stop_reason if response.stop_reason in _STOP_REASONS else "end_turn"

    return LLMResponse(
        message=Message(
            role="assistant",
            content="\n".join(text_parts),
            tool_calls=tool_calls or None,
        ),
        stop_reason=stop_reason,
        usage=TokenUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        ),
    )
