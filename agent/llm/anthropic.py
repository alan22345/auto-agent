"""Anthropic Claude API provider.

Message <-> Anthropic-API translation lives in `agent.llm.anthropic_mapper`;
this provider is a thin transport+auth adapter that holds the API-key client
and exposes the SDK's native token counter.
"""

from __future__ import annotations

from typing import Any

import anthropic

from agent.llm.anthropic_mapper import from_api_response, to_api_messages, to_api_tool
from agent.llm.base import LLMProvider
from agent.llm.types import (
    LLMResponse,
    Message,
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

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": to_api_messages(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [to_api_tool(t) for t in tools]

        response = await self._client.messages.create(**kwargs)
        return from_api_response(response)

    async def count_tokens(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
    ) -> int:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": to_api_messages(messages),
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [to_api_tool(t) for t in tools]

        try:
            result = await self._client.messages.count_tokens(**kwargs)
            return result.input_tokens
        except Exception:
            # Fallback to rough estimate if the count_tokens endpoint fails.
            total = ""
            if system:
                total += system
            for m in messages:
                total += m.content or ""
            return self.rough_token_count(total)
