"""Multi-tier token counting: API-accurate -> rough local estimate."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from agent.llm.base import LLMProvider
    from agent.llm.types import Message, ToolDefinition

logger = structlog.get_logger()

# Rough estimate: ~1 token per 4 chars, with 1.33x safety padding
_CHARS_PER_TOKEN = 4
_SAFETY_FACTOR = 1.33
# Flat token estimate for images
_IMAGE_TOKEN_ESTIMATE = 2000


class TokenCounter:
    """Provides token counting with API fallback to rough estimation."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        self._api_available = True  # Disable if API counting repeatedly fails

    async def count(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
    ) -> int:
        """Count tokens using the provider API, falling back to rough estimate."""
        if self._api_available:
            try:
                return await self._provider.count_tokens(messages, system, tools)
            except Exception as e:
                logger.warning("token_count_api_failed", error=str(e))
                self._api_available = False

        return self._rough_count(messages, system, tools)

    def rough_estimate(self, text: str) -> int:
        """Fast local estimate for a single string."""
        if not text:
            return 0
        return int(len(text) / _CHARS_PER_TOKEN * _SAFETY_FACTOR)

    def estimate_messages(self, messages: list[Message]) -> int:
        """Rough estimate across a list of messages (no API call)."""
        return self._rough_count(messages)

    def _rough_count(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
    ) -> int:
        total = 0
        if system:
            total += self.rough_estimate(system)
        for msg in messages:
            total += self.rough_estimate(msg.content)
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    # Tool call arguments as JSON
                    total += self.rough_estimate(str(tc.arguments))
                    total += self.rough_estimate(tc.name)
            # Per-message overhead (role, formatting)
            total += 4
        if tools:
            for tool in tools:
                total += self.rough_estimate(tool.description)
                total += self.rough_estimate(str(tool.parameters))
        return total
