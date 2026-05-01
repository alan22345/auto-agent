"""Generate a short title from the first user message in a search session."""

from __future__ import annotations

import asyncio

import structlog

from agent.llm import get_provider
from agent.llm.types import Message

logger = structlog.get_logger()

_SYSTEM = (
    "You generate short titles (2-6 words) for search sessions. "
    "Output the title only, no quotes, no punctuation at the end."
)

# Cap the title call so a slow provider can't block the whole turn's
# persistence. If we time out, fall back to the truncated user message.
_TIMEOUT_SECONDS = 8.0


async def generate_title(first_message: str) -> str:
    """Generate a short title for a search session, or fall back to a slice
    of the user's message on any error or timeout.

    Always uses Bedrock (matches the search loop) — the configured default
    provider may be claude_cli, which is a CLI passthrough that hangs
    indefinitely in this one-shot context.
    """
    fallback = (first_message or "").strip()[:80] or "New search"
    try:
        provider = get_provider(provider_override="bedrock", model_override="fast")
        response = await asyncio.wait_for(
            provider.complete(
                messages=[Message(role="user", content=first_message)],
                system=_SYSTEM,
            ),
            timeout=_TIMEOUT_SECONDS,
        )
        title = (response.message.content or "").strip().strip('"').strip("'")
        return title[:80] or fallback
    except (Exception, asyncio.TimeoutError) as e:
        logger.warning("generate_title_failed", error=str(e))
        return fallback
