"""Structured intent extraction — what the user actually wants.

Used by the coding phase to decorate the system/coding prompts with a
``change_type`` (bugfix vs feature vs refactor etc.) and target areas.
Non-blocking on failure: returns ``{}`` and the pipeline continues.

Skipped entirely when ``settings.llm_provider == "claude_cli"`` because the
CLI provider understands the task natively.
"""

from __future__ import annotations

import json as _json

from agent.llm import get_provider
from agent.llm.types import Message
from shared.config import settings
from shared.logging import setup_logging

log = setup_logging("agent.lifecycle.intent")


INTENT_EXTRACTION_PROMPT = """\
Analyze this task and extract structured intent. Output ONLY a JSON object, no other text.

Task title: {title}
Task description: {description}

JSON format:
{{
  "change_type": "bugfix|feature|refactor|config|docs|test|performance",
  "target_areas": "comma-separated file paths or module areas likely involved",
  "acceptance_criteria": "what must be true when the task is done (1-2 sentences)",
  "constraints": "what should NOT be changed or any restrictions (1 sentence, or empty string)"
}}

Rules:
- change_type: pick the single best category
- target_areas: infer from the description — name specific files/modules if mentioned, otherwise name the likely area (e.g. "authentication", "database layer")
- acceptance_criteria: concrete, testable conditions — not vague ("works correctly")
- constraints: only include if the description implies restrictions; empty string otherwise
- Output ONLY the JSON. No markdown fences, no explanation.
"""


async def extract_intent(title: str, description: str) -> dict:
    """Extract structured intent from a task title and description.

    Returns a dict with keys: change_type, target_areas, acceptance_criteria, constraints.
    Returns empty dict on any failure (non-blocking — the pipeline continues without it).
    """
    # Intent extraction is redundant when using Claude CLI — it understands
    # the task natively. Only useful for API providers where we control the loop.
    if settings.llm_provider == "claude_cli":
        return {}
    try:
        provider = get_provider(model_override="fast")
        response = await provider.complete(
            messages=[
                Message(
                    role="user",
                    content=INTENT_EXTRACTION_PROMPT.format(title=title, description=description),
                )
            ],
            max_tokens=300,
        )
        text = response.message.content.strip()
        # Strip markdown fences if the LLM wraps the JSON
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        return _json.loads(text)
    except Exception:
        log.warning("intent_extraction_failed", title=title[:80])
        return {}
