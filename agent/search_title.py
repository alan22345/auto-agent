"""Generate a short title from the first user message in a search session."""

from __future__ import annotations

import structlog

from agent.llm import get_provider
from agent.llm.types import Message

logger = structlog.get_logger()

_SYSTEM = (
    "You generate short titles (2-6 words) for search sessions. "
    "Output the title only, no quotes, no punctuation at the end."
)


async def generate_title(first_message: str) -> str:
    """Generate a short title for a search session, or fall back to a slice
    of the user's message on any error."""
    fallback = (first_message or "").strip()[:80] or "New search"
    try:
        provider = get_provider()
        response = await provider.complete(
            messages=[Message(role="user", content=first_message)],
            system=_SYSTEM,
        )
        title = (response.message.content or "").strip().strip('"').strip("'")
        return title[:80] or fallback
    except Exception as e:
        logger.warning("generate_title_failed", error=str(e))
        return fallback
