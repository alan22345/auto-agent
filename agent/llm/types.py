"""Model-agnostic message and tool-call types shared across all providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolDefinition:
    """Schema sent to the model so it knows how to call a tool."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object


@dataclass
class Message:
    """A single conversation message (user, assistant, system, or tool result)."""

    role: Literal["user", "assistant", "system", "tool"]
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None  # Set when role == "tool"
    tool_name: str | None = None  # Name of the tool that produced this result
    token_estimate: int | None = None  # Rough token count (for compaction tracking)


@dataclass
class TokenUsage:
    """Token consumption for a single LLM call."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    message: Message
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "error"]
    usage: TokenUsage = field(default_factory=TokenUsage)


# Context window sizes for common models.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic
    "claude-opus-4-20250514": 200_000,
    "claude-sonnet-4-20250514": 200_000,
    "claude-haiku-4-20250506": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "o3": 200_000,
    "o4-mini": 200_000,
}

DEFAULT_CONTEXT_WINDOW = 128_000


def context_window_for_model(model: str) -> int:
    """Look up the context window for a model, falling back to a safe default."""
    for key, size in MODEL_CONTEXT_WINDOWS.items():
        if key in model:
            return size
    return DEFAULT_CONTEXT_WINDOW
