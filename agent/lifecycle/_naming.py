"""Branch slug, PR title, and session ID generation.

Two session-ID helpers codify the policy locked in by tests/test_session_id.py:

  ``_session_id`` \u2014 DETERMINISTIC. Resume across handler invocations is the
  whole point of the planning → coding → clarification → review-fix lifecycle.
  Concurrency is prevented by handler-level guards inside the lifecycle
  modules (e.g. ``_active_planning`` in ``planning.py``).

  ``_fresh_session_id`` \u2014 UNIQUE PER CALL. Used by one-shot agents that are
  NOT designed to resume. The Claude CLI provider tracks live session IDs and
  rejects re-use with "Session ID … is already in use", so a deterministic
  hash collides on retry.
"""

from __future__ import annotations

import re as _re
import uuid

from agent.llm import get_provider
from agent.llm.types import Message


async def _slugify_llm(title: str, max_len: int = 40) -> str:
    """Use the LLM to generate a concise branch slug."""
    try:
        provider = get_provider()
        response = await provider.complete(
            messages=[
                Message(
                    role="user",
                    content=(
                        f"Generate a short git branch slug (2-4 words, lowercase, hyphenated, no special chars) "
                        f"that captures the essence of this task. Reply with ONLY the slug, nothing else.\n\n"
                        f"Task: {title[:200]}"
                    ),
                )
            ],
            max_tokens=50,
        )
        slug = response.message.content.strip().lower()
        slug = _re.sub(r"[^a-z0-9-]", "", slug)
        slug = _re.sub(r"-+", "-", slug).strip("-")
        if 3 <= len(slug) <= max_len:
            return slug
    except Exception:
        pass
    return _slugify_fallback(title, max_len)


def _slugify_fallback(title: str, max_len: int = 40) -> str:
    """Mechanical fallback slugify."""
    cleaned = _re.sub(
        r"^repo\s*[-\u2013\u2014]\s*\S+\s*[-\u2013\u2014]\s*",
        "",
        title,
        flags=_re.IGNORECASE,
    ).strip()
    cleaned = _re.sub(r"[^a-z0-9\s]", "", cleaned.lower())
    slug = _re.sub(r"\s+", "-", cleaned.strip())
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]
    return slug or "task"


async def _branch_name(task_id: int, title: str) -> str:
    slug = await _slugify_llm(title)
    return f"auto-agent/{slug}-{task_id}"


async def _pr_title(title: str) -> str:
    """Generate a clean PR title using the LLM."""
    cleaned = _re.sub(
        r"^repo\s*[-\u2013\u2014]\s*\S+\s*[-\u2013\u2014]\s*",
        "",
        title,
        flags=_re.IGNORECASE,
    ).strip()
    try:
        provider = get_provider()
        response = await provider.complete(
            messages=[
                Message(
                    role="user",
                    content=(
                        f"Write a concise PR title (under 60 chars) for this task. "
                        f"Reply with ONLY the title, nothing else.\n\nTask: {cleaned[:300]}"
                    ),
                )
            ],
            max_tokens=80,
        )
        pr_title = response.message.content.strip()
        if 5 <= len(pr_title) <= 80:
            return f"[auto-agent] {pr_title}"
    except Exception:
        pass
    return f"[auto-agent] {cleaned[:100]}"


def _session_id(task_id: int, created_at: str | None = None) -> str:
    """Deterministic UUID session ID for a task.

    Stable across handler invocations so the planning → coding → clarification
    → review-fix lifecycle can resume the same session. Lifecycle handlers
    are guarded against concurrent re-entry (``_active_planning``,
    ``_active_clarification_tasks``) so a second handler can't race the first
    on the same session ID.
    """
    seed = f"auto-agent-task-{task_id}-{created_at or ''}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def _fresh_session_id(task_id: int, label: str) -> str:
    """Per-invocation UUID for agents that are designed NOT to resume.

    The independent reviewer (and any other one-shot agent) needs a fresh
    session every call. A deterministic hash collides on retry: the Claude
    CLI provider tracks live session IDs and rejects re-use with
    "Session ID ... is already in use." We include task_id + label in
    the seed for log readability and uuid4().hex for uniqueness.
    """
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"auto-agent-{label}-{task_id}-{uuid.uuid4().hex}",
        )
    )
