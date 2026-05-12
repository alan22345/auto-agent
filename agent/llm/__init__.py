"""LLM provider abstraction — swap models via configuration."""

from __future__ import annotations

from agent.llm.base import LLMProvider

# Model tiers for cost-optimized routing
MODEL_TIERS = {
    "fast": "claude-haiku-4-5",       # Cheap: mechanical tasks, summaries, naming
    "standard": "claude-sonnet-4-6",   # Default: implementation, reviews
    "capable": "claude-opus-4-6",      # Expensive: architecture, complex debugging
}


def get_provider(
    model_override: str | None = None,
    provider_override: str | None = None,
    home_dir: str | None = None,
    api_key_override: str | None = None,
) -> LLMProvider:
    """Return the configured LLM provider instance.

    Args:
        model_override: Override the configured model. Can be a model name
                       or a tier name ("fast", "standard", "capable").
        provider_override: Force a specific provider ("anthropic", "bedrock",
                       "claude_cli"), ignoring `settings.llm_provider`. Used
                       by flows that require an API provider with native tool
                       calling (e.g. the Search tab — claude_cli passthrough
                       skips the agentic loop and can't stream tool events).
        api_key_override: Anthropic API key to use instead of
                       ``settings.anthropic_api_key``. Per-user keys (Phase 1
                       multi-tenant) are resolved by the caller via
                       ``shared.secrets.get(user_id, "anthropic_api_key")``
                       and threaded in here. Ignored on the Bedrock and
                       claude_cli paths — those are infra-level.
    """
    from shared.config import settings

    model = model_override or settings.llm_model
    # Resolve tier names to actual model IDs
    model = MODEL_TIERS.get(model, model)
    provider = provider_override or settings.llm_provider

    if provider == "anthropic":
        from agent.llm.anthropic import AnthropicProvider

        return AnthropicProvider(
            api_key=api_key_override or settings.anthropic_api_key,
            model=model,
        )
    elif provider == "bedrock":
        from agent.llm.bedrock import BedrockProvider

        return BedrockProvider(
            region=settings.bedrock_region,
            model=model,
            bearer_token=settings.aws_bearer_token_bedrock,
            aws_access_key=settings.aws_access_key_id,
            aws_secret_key=settings.aws_secret_access_key,
            aws_session_token=settings.aws_session_token,
        )
    elif provider == "claude_cli":
        from agent.llm.claude_cli import ClaudeCLIProvider

        # Default-route system-level callers (no explicit home_dir) to the
        # configured fallback user's vault. Without this, the CLI runs against
        # the container's default HOME, which has no valid credentials once
        # the legacy /home/node/.claude bind-mount is gone.
        if home_dir is None:
            from orchestrator.claude_auth import (
                ensure_vault_dir as _ensure_vault_dir,
            )
            from orchestrator.claude_auth import (
                fallback_user_id as _fallback_user_id,
            )

            fid = _fallback_user_id()
            if fid is not None:
                home_dir = _ensure_vault_dir(fid)

        return ClaudeCLIProvider(home_dir=home_dir)
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


async def resolve_user_anthropic_key(user_id: int | None) -> str | None:
    """Look up the per-user Anthropic API key, or ``None`` if unset.

    Pass the result as ``api_key_override`` to ``get_provider``. Returns
    ``None`` for ``user_id is None`` or any lookup failure — the caller
    falls back to ``settings.anthropic_api_key``.
    """
    if user_id is None:
        return None
    try:
        from shared import secrets as _secrets

        return await _secrets.get(user_id, "anthropic_api_key")
    except Exception:
        return None
