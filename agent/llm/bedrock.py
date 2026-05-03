"""AWS Bedrock provider — uses the anthropic SDK's built-in Bedrock support.

Authentication uses the standard AWS credential chain:
- Production (ECS/Lambda): IAM task role
- Local: AWS SSO / ~/.aws/credentials
No explicit access keys needed.

Message <-> Anthropic-API translation lives in `agent.llm.anthropic_mapper`;
this provider is a thin transport+auth adapter that adds AWS client setup and
a retry loop for transient throttling errors.
"""

from __future__ import annotations

import asyncio
import os
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

# Transient errors that should be retried with exponential backoff.
# 503 = Bedrock unavailable/throttled. 429 = rate limit. 529 = Anthropic overload.
_RETRYABLE_STATUS_CODES = {429, 503, 529}
_MAX_RETRIES = 4
_INITIAL_BACKOFF_S = 2.0

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
        kwargs: dict[str, Any] = {
            "model": self._bedrock_model,
            "messages": to_api_messages(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [to_api_tool(t) for t in tools]

        # Retry on transient errors (throttling, service unavailable).
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await self._client.messages.create(**kwargs)
                return from_api_response(response)
            except anthropic.APIStatusError as e:
                last_exc = e
                if e.status_code in _RETRYABLE_STATUS_CODES and attempt < _MAX_RETRIES:
                    backoff = _INITIAL_BACKOFF_S * (2**attempt)
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
        # Bedrock doesn't have a token counting endpoint — use rough estimate.
        total = ""
        if system:
            total += system
        for m in messages:
            total += m.content or ""
        return self.rough_token_count(total)
