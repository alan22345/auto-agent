"""LLM provider abstraction — swap models via configuration."""

from __future__ import annotations

from agent.llm.base import LLMProvider

# Model tiers for cost-optimized routing
MODEL_TIERS = {
    "fast": "claude-haiku-4-5",       # Cheap: mechanical tasks, summaries, naming
    "standard": "claude-sonnet-4-6",   # Default: implementation, reviews
    "capable": "claude-opus-4-6",      # Expensive: architecture, complex debugging
}


def get_provider(model_override: str | None = None) -> LLMProvider:
    """Return the configured LLM provider instance.

    Args:
        model_override: Override the configured model. Can be a model name
                       or a tier name ("fast", "standard", "capable").
    """
    from shared.config import settings

    model = model_override or settings.llm_model
    # Resolve tier names to actual model IDs
    model = MODEL_TIERS.get(model, model)

    if settings.llm_provider == "anthropic":
        from agent.llm.anthropic import AnthropicProvider

        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=model,
        )
    elif settings.llm_provider == "bedrock":
        from agent.llm.bedrock import BedrockProvider

        return BedrockProvider(
            region=settings.bedrock_region,
            model=model,
            bearer_token=settings.aws_bearer_token_bedrock,
            aws_access_key=settings.aws_access_key_id,
            aws_secret_key=settings.aws_secret_access_key,
            aws_session_token=settings.aws_session_token,
        )
    elif settings.llm_provider == "claude_cli":
        from agent.llm.claude_cli import ClaudeCLIProvider

        return ClaudeCLIProvider()
    else:
        raise ValueError(f"Unknown LLM provider: {settings.llm_provider}")
