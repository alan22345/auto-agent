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


def message_from_dict(m: dict[str, Any]) -> Message:
    """Reconstruct a :class:`Message` from its ``asdict()`` form.

    Round-tripping through JSON storage (e.g.
    ``messenger_conversations.messages_json``) loses dataclass typing on
    ``tool_calls`` — it comes back as ``list[dict]``. Splatting via
    ``Message(**m)`` stores the dicts as-is, then any wire-format mapper
    that expects ``ToolCall`` instances crashes on ``tc.id``. Use this
    helper instead so ``tool_calls`` is rehydrated correctly.
    """
    tool_calls_raw = m.get("tool_calls")
    tool_calls: list[ToolCall] | None = None
    if tool_calls_raw:
        tool_calls = [ToolCall(**tc) for tc in tool_calls_raw]
    return Message(
        role=m["role"],
        content=m.get("content", ""),
        tool_calls=tool_calls,
        tool_call_id=m.get("tool_call_id"),
        tool_name=m.get("tool_name"),
        token_estimate=m.get("token_estimate"),
    )


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
