"""AWS Bedrock provider — uses the anthropic SDK's built-in Bedrock support.

Authentication uses the standard AWS credential chain:
- Production (ECS/Lambda): IAM task role
- Local: AWS SSO / ~/.aws/credentials
No explicit access keys needed.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import anthropic

# Transient errors that should be retried with exponential backoff.
# 503 = Bedrock unavailable/throttled. 429 = rate limit.
_RETRYABLE_STATUS_CODES = {429, 503, 529}
_MAX_RETRIES = 4
_INITIAL_BACKOFF_S = 2.0

from agent.llm.base import LLMProvider
from agent.llm.types import (
    LLMResponse,
    Message,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    context_window_for_model,
)

# Map friendly model names to Bedrock inference profile IDs.
# Use the latest cross-region profiles (us.*) for highest throughput.
BEDROCK_MODEL_MAP: dict[str, str] = {
    # Latest models (use these)
    "claude-sonnet-4-6": "us.anthropic.claude-sonnet-4-6",
    "claude-opus-4-6": "us.anthropic.claude-opus-4-6-v1",
    "claude-haiku-4-5": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    # Older Sonnet/Opus 4.0 (lower quotas)
    "claude-sonnet-4-20250514": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-opus-4-20250514": "us.anthropic.claude-opus-4-20250514-v1:0",
}


class BedrockProvider(LLMProvider):
    """LLM provider using AWS Bedrock via the anthropic SDK."""

    is_passthrough = False

    def __init__(
        self,
        region: str = "us-east-1",
        model: str = "claude-sonnet-4-20250514",
        aws_access_key: str = "",
        aws_secret_key: str = "",
        aws_session_token: str = "",
        bearer_token: str = "",
    ):
        self.model = model
        self.max_context_tokens = context_window_for_model(model)
        self._bedrock_model = BEDROCK_MODEL_MAP.get(model, model)

        # Authentication priority:
        # 1. Bedrock API key (bearer token) — set AWS_BEARER_TOKEN_BEDROCK in env
        # 2. Explicit IAM access keys
        # 3. Fall back to AWS credential chain (~/.aws/, SSO, instance role)
        if bearer_token:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = bearer_token
            self._client = anthropic.AsyncAnthropicBedrock(aws_region=region)
        elif aws_access_key and aws_secret_key:
            kwargs: dict[str, str] = {
                "aws_region": region,
                "aws_access_key": aws_access_key,
                "aws_secret_key": aws_secret_key,
            }
            if aws_session_token:
                kwargs["aws_session_token"] = aws_session_token
            self._client = anthropic.AsyncAnthropicBedrock(**kwargs)
        else:
            self._client = anthropic.AsyncAnthropicBedrock(aws_region=region)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        api_messages = self._build_api_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self._bedrock_model,
            "messages": api_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [self._to_api_tool(t) for t in tools]

        # Retry on transient errors (throttling, service unavailable)
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await self._client.messages.create(**kwargs)
                return self._from_api_response(response)
            except anthropic.APIStatusError as e:
                last_exc = e
                if e.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                    backoff = _INITIAL_BACKOFF_S * (2 ** attempt)
                    await asyncio.sleep(backoff)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    async def count_tokens(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
    ) -> int:
        # Bedrock doesn't have a token counting endpoint — use rough estimate
        total = ""
        if system:
            total += system
        for m in messages:
            total += m.content or ""
        return self.rough_token_count(total)

    # ------------------------------------------------------------------
    # Message format conversion (same as Anthropic — same API format)
    # ------------------------------------------------------------------

    def _build_api_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert our Messages to Anthropic API format, BATCHING consecutive
        tool results into a single user message.

        Anthropic's API requires all tool_results responding to a single
        assistant turn's tool_uses to be grouped in ONE user message's
        content array — not split across multiple user messages. Splitting
        them silently breaks the conversation: the assistant may think its
        tool calls weren't answered and repeat them, causing a read loop.
        """
        api_messages: list[dict[str, Any]] = []
        pending_tool_results: list[dict[str, Any]] = []

        def flush_tool_results():
            if pending_tool_results:
                api_messages.append({
                    "role": "user",
                    "content": list(pending_tool_results),
                })
                pending_tool_results.clear()

        for msg in messages:
            if msg.role == "system":
                continue

            if msg.role == "tool":
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                })
                continue

            # Any non-tool message flushes pending tool_results first
            flush_tool_results()

            if msg.role == "assistant" and msg.tool_calls:
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
                api_messages.append({"role": "assistant", "content": content})
            else:
                api_messages.append({"role": msg.role, "content": msg.content})

        # Trailing tool results (shouldn't normally happen but be safe)
        flush_tool_results()
        return api_messages

    def _to_api_tool(self, tool: ToolDefinition) -> dict[str, Any]:
        schema = tool.parameters
        # Bedrock requires "type": "object" on all tool input schemas
        if isinstance(schema, dict) and "type" not in schema:
            schema = {**schema, "type": "object"}
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": schema,
        }

    def _from_api_response(self, response: Any) -> LLMResponse:
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
