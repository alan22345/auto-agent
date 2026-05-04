"""Single-call LLM extractor: source text → proposed team-memory facts."""

from __future__ import annotations

import uuid

import structlog

from agent.llm import get_provider
from agent.llm.structured import complete_json
from agent.llm.types import Message
from shared.types import ConflictInfo, ProposedFact

logger = structlog.get_logger()

_ALLOWED_KINDS = {"decision", "architecture", "gotcha", "status", "preference", "fact"}

_SYSTEM_PROMPT = """You extract structured facts for a team-memory knowledge graph.

Given source text, return STRICT JSON with this exact shape (no prose, no code fences):

{"facts": [
  {"entity": "<name>", "entity_type": "<project|concept|person|repo|system>",
   "kind": "<decision|architecture|gotcha|status|preference|fact>",
   "content": "<one concise fact, 1-2 sentences>",
   "conflicts_with": ["<existing_fact_id>", ...]
  }
]}

Rules:
- Each fact must be a self-contained statement that makes sense without the source doc.
- Prefer concrete, load-bearing information: decisions with their why, gotchas with their symptom, statuses with their date.
- Do NOT repeat facts that already exist in the provided "Existing facts" section unless genuinely correcting them.
- Only set conflicts_with when the new content directly contradicts (not augments) an existing fact.
- If you cannot extract anything useful, return {"facts": []}.
"""


def _build_user_message(text: str, hint: str | None, existing: dict[str, list[dict]]) -> str:
    parts: list[str] = []
    if hint:
        parts.append(f"CONTEXT HINT: {hint}\n")
    if existing:
        parts.append("EXISTING FACTS (for conflict checking):")
        for entity, facts in existing.items():
            parts.append(f"- Entity: {entity}")
            for f in facts:
                parts.append(f"  - id={f['id']} kind={f.get('kind', '?')}: {f['content']}")
        parts.append("")
    parts.append("SOURCE TEXT:")
    parts.append(text)
    return "\n".join(parts)


def _to_proposed(raw: dict, existing_by_id: dict[str, dict]) -> ProposedFact:
    kind = raw.get("kind", "fact")
    if kind not in _ALLOWED_KINDS:
        kind = "fact"
    conflicts: list[ConflictInfo] = []
    for fact_id in raw.get("conflicts_with") or []:
        existing = existing_by_id.get(fact_id)
        if existing:
            conflicts.append(ConflictInfo(fact_id=fact_id, existing_content=existing["content"]))
    return ProposedFact(
        row_id=f"r-{uuid.uuid4().hex[:8]}",
        entity=raw.get("entity", "").strip() or "unknown",
        entity_type=raw.get("entity_type", "concept"),
        kind=kind,
        content=raw.get("content", "").strip(),
        conflicts=conflicts,
    )


async def extract(
    text: str,
    hint: str | None,
    existing_facts_by_entity: dict[str, list[dict]],
    provider=None,
) -> list[ProposedFact]:
    """Run one structured-output LLM call and return proposed facts.

    Retries once on malformed JSON; raises ValueError after the second failure.
    """
    if provider is None:
        provider = get_provider()

    existing_by_id: dict[str, dict] = {}
    for facts in existing_facts_by_entity.values():
        for f in facts:
            existing_by_id[f["id"]] = f

    user_message = _build_user_message(text, hint, existing_facts_by_entity)
    messages = [Message(role="user", content=user_message)]

    try:
        data = await complete_json(
            provider,
            messages=messages,
            system=_SYSTEM_PROMPT,
            max_tokens=4096,
            retries=2,
        )
    except ValueError as e:
        logger.warning("memory_extract_parse_failed", error=str(e))
        raise ValueError(f"could not parse extractor response: {e}") from e

    facts_raw = data.get("facts", [])
    if not isinstance(facts_raw, list):
        raise ValueError("'facts' is not a list")
    return [_to_proposed(f, existing_by_id) for f in facts_raw]
