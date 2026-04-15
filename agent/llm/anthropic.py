"""Anthropic Claude API provider."""

from __future__ import annotations

import json
import uuid
from typing import Any

import anthropic

from agent.llm.base import LLMProvider
from agent.llm.types import (
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    context_window_for_model,
)


class AnthropicProvider(LLMProvider):
    """LLM provider using the Anthropic Messages API."""

    is_passthrough = False

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        self.model = model
        self.max_context_tokens = context_window_for_model(model)
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        api_messages = [self._to_api_message(m) for m in messages if m.role != "system"]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [self._to_api_tool(t) for t in tools]

        response = await self._client.messages.create(**kwargs)
        return self._from_api_response(response)

    async def count_tokens(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
    ) -> int:
        api_messages = [self._to_api_message(m) for m in messages if m.role != "system"]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [self._to_api_tool(t) for t in tools]

        try:
            result = await self._client.messages.count_tokens(**kwargs)
            return result.input_tokens
        except Exception:
            # Fallback to rough estimate
            total = ""
            if system:
                total += system
            for m in messages:
                total += m.content or ""
            return self.rough_token_count(total)

    # ------------------------------------------------------------------
    # Message format conversion
    # ------------------------------------------------------------------

    def _to_api_message(self, msg: Message) -> dict[str, Any]:
        """Convert our Message to Anthropic API format."""
        if msg.role == "assistant" and msg.tool_calls:
            # Assistant message with tool use blocks
            content: list[dict[str, Any]] = []
            if msg.content:
                content.append({"type": "text", "text": msg.content})
            for tc in msg.tool_calls:
                content.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments,
                })
            return {"role": "assistant", "content": content}

        if msg.role == "tool":
            # Tool result message
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                ],
            }

        # Plain user or assistant text
        return {"role": msg.role, "content": msg.content}

    def _to_api_tool(self, tool: ToolDefinition) -> dict[str, Any]:
        """Convert our ToolDefinition to Anthropic API format."""
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }

    def _from_api_response(self, response: Any) -> LLMResponse:
        """Convert an Anthropic API response to our LLMResponse."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else json.loads(block.input),
                    )
                )

        stop_reason_map = {
            "end_turn": "end_turn",
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
        }
        stop = stop_reason_map.get(response.stop_reason, "end_turn")

        return LLMResponse(
            message=Message(
                role="assistant",
                content="\n".join(text_parts),
                tool_calls=tool_calls or None,
            ),
            stop_reason=stop,
            usage=TokenUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
        )
