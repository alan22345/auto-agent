"""Structured LLM output — single owner of "LLM text reply -> dict".

Five callers in the codebase all share the same shape: the model is asked
to reply with a JSON object, sometimes wraps it in ``` fences, sometimes
adds a prose preamble, sometimes returns garbage. Each caller used to
re-implement strip-fence + locate-braces + decode + recover differently
(``po_analyzer`` and ``improvement_agent`` — formerly ``architect_analyzer``
— were byte-for-byte identical; ``classifier`` skipped fence-stripping;
``memory_extractor`` had its own retry loop). This module collapses all
of that into:

- ``parse_json_response(text)`` — pure: returns ``dict | None``. Never
  raises. Callers pick their own fallback policy: ``None`` -> early-return
  (po_analyzer, improvement_agent), ``None`` -> ``{}`` (intent
  extraction), ``None`` -> heuristic fallback (classifier).
- ``complete_json(provider, ...)`` — one-shot LLM call + parse + bounded
  retry with a "your last response wasn't valid JSON" nudge appended to
  ``system``. Raises ``ValueError`` after ``retries`` failures.

Design choice: a top-level JSON list is rejected (returns ``None``). All
five callers expect an object; silently handing back a list would be a
shape mismatch waiting to bite a downstream ``data.get(...)``.

This module sits beside ``anthropic_mapper.py`` in ``agent/llm/`` — the
LLM seam now hosts both "chat completion" (the mapper) and "structured
one-shot" (this file). Both are real adapters: see ADR-010.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.llm.base import LLMProvider
    from agent.llm.types import Message


_RETRY_NUDGE = (
    "\n\nYour previous response was not valid JSON. "
    "Return ONLY a JSON object now — no prose, no markdown fences."
)


def parse_json_response(text: str) -> dict | None:
    """Extract a JSON object from an LLM text reply.

    Handles bare JSON, JSON wrapped in ``` fences (with or without a
    ``json`` language tag), and JSON preceded by a prose preamble (via
    first-``{`` / last-``}`` brace location).

    Returns ``None`` (never raises) on:
      - empty / whitespace-only input,
      - no ``{`` / ``}`` pair found,
      - slice between braces fails to decode,
      - top-level value is not an object (e.g. a JSON list).
    """
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None

    # Strip a leading ```... line (with or without a language tag) and a
    # trailing ``` line. This handles both ``` and ```json.
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None

    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None
    return parsed


async def complete_json(
    provider: LLMProvider,
    messages: list[Message],
    *,
    system: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    retries: int = 2,
) -> dict:
    """One-shot ``provider.complete`` + ``parse_json_response`` + retry.

    On failure, retries up to ``retries`` total attempts. On attempt 2+,
    a "your previous response wasn't valid JSON" nudge is appended to
    ``system``.

    Raises ``ValueError`` when no attempt produced parseable JSON.
    """
    last_raw = ""
    for attempt in range(1, max(1, retries) + 1):
        attempt_system = system if attempt == 1 else (system or "") + _RETRY_NUDGE
        response = await provider.complete(
            messages=messages,
            system=attempt_system,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        last_raw = response.message.content or ""
        parsed = parse_json_response(last_raw)
        if parsed is not None:
            return parsed

    snippet = last_raw[:200].replace("\n", " ")
    raise ValueError(
        f"complete_json: could not parse response after {retries} attempts: {snippet!r}"
    )
