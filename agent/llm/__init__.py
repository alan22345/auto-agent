"""LLM provider abstraction — swap models via configuration."""

from __future__ import annotations

from agent.llm.base import LLMProvider


def get_provider() -> LLMProvider:
    """Return the configured LLM provider instance."""
    from shared.config import settings

    if settings.llm_provider == "anthropic":
        from agent.llm.anthropic import AnthropicProvider

        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=settings.llm_model,
        )
    elif settings.llm_provider == "openai":
        from agent.llm.openai import OpenAIProvider

        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=settings.llm_model,
        )
    elif settings.llm_provider == "claude_cli":
        from agent.llm.claude_cli import ClaudeCLIProvider

        return ClaudeCLIProvider()
    else:
        raise ValueError(f"Unknown LLM provider: {settings.llm_provider}")
