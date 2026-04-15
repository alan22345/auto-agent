"""Abstract LLM provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from agent.llm.types import LLMResponse, Message, ToolDefinition


class LLMProvider(ABC):
    """Abstract interface for language model providers.

    Implementations must map our model-agnostic types to provider-specific
    API formats and back.
    """

    model: str
    max_context_tokens: int
    is_passthrough: bool = False  # True for CLI providers that handle tools internally

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Send messages to the model and return a response.

        Args:
            messages: Conversation history.
            tools: Available tool definitions (ignored by passthrough providers).
            system: System prompt text.
            max_tokens: Maximum output tokens.
            temperature: Sampling temperature.
        """

    @abstractmethod
    async def count_tokens(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolDefinition] | None = None,
    ) -> int:
        """Return an accurate token count for the given messages + system + tools."""

    def rough_token_count(self, text: str) -> int:
        """Fast local estimate: ~1 token per 4 chars with 1.33x safety padding."""
        return int(len(text) / 4 * 1.33) if text else 0
