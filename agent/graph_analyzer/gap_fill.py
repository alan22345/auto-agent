"""One-shot LLM gap-fill for unresolved dispatch sites (ADR-016 Phase 3 §3).

For each :class:`UnresolvedSite` the parser surfaces, we feed the LLM
the surrounding source plus the candidate node ids and ask it to emit
zero or more ``calls`` edges in a tight JSON shape::

    {"edges": [
        {"target_node_id": "...", "evidence_line": 42,
         "evidence_snippet": "handler = HANDLERS[event_type]"},
        ...
    ]}

The LLM is told: stay inside the candidate list, cite the line where
the call happens, and return ``edges: []`` if you cannot resolve.

This module returns the constructed :class:`shared.types.Edge` instances
*without* running citation/target validation — that is the caller's job
(see ``agent/graph_analyzer/pipeline.py``). The only soft filter applied
here is "target id must be in the candidate pool"; everything else is
deferred to the unconditional validators.

Cost discipline (per the Phase 3 brief):

* The candidate-pool size is capped at :data:`_CANDIDATE_CAP` (150 by
  default) before we build the prompt.
* Output is bounded to :data:`_GAP_FILL_MAX_TOKENS` (1024) — gap-fill
  has a tight, structured shape; it does not need 4k tokens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel, Field, ValidationError

from agent.llm.structured import complete_json
from agent.llm.types import Message
from shared.types import Edge, EdgeEvidence

if TYPE_CHECKING:
    from agent.graph_analyzer.types import UnresolvedSite
    from agent.llm.base import LLMProvider
    from shared.types import Node

log = structlog.get_logger(__name__)

#: Hard cap on the number of candidate node ids included in the LLM
#: prompt. Too-large candidate lists balloon tokens for little benefit —
#: the LLM only ever cites one or two per site. 150 is enough to cover
#: a moderately-sized area without crowding the prompt.
_CANDIDATE_CAP = 150

#: Output token budget for gap-fill. The JSON shape is small; we never
#: need 4k. Keeping this tight makes the gap-fill cost predictable.
_GAP_FILL_MAX_TOKENS = 1024


class _LLMEdgePayload(BaseModel):
    """Pydantic schema for one edge as emitted by the LLM. Malformed
    entries (missing fields, wrong types) ValidationError out and get
    dropped silently — they cannot produce edges."""

    target_node_id: str = Field(min_length=1)
    evidence_line: int = Field(ge=1)
    evidence_snippet: str = Field(min_length=1)


def _build_system_prompt(
    site: UnresolvedSite,
    candidate_ids: list[str],
) -> str:
    """Build the system prompt for one gap-fill call."""
    bulleted = "\n".join(f"- {nid}" for nid in candidate_ids)
    return (
        "You resolve dynamic-dispatch call sites in a code graph.\n"
        "\n"
        "The user message shows ONE unresolved call site in a Python "
        "codebase, with ~30 lines of surrounding source. Static "
        "analysis could not determine which function the call lands on. "
        "Your job: identify the resolved target(s) by reading the "
        f"surrounding code. The dispatch pattern is hinted as: {site.pattern_hint}.\n"
        "\n"
        "OUTPUT — return ONLY a JSON object with this shape, no prose, no "
        "markdown fences:\n"
        '{"edges": [\n'
        '  {"target_node_id": "<id from the candidate list below>",\n'
        '   "evidence_line": <1-indexed line in the file where the call happens>,\n'
        '   "evidence_snippet": "<the source line, copied verbatim>"},\n'
        "  ...\n"
        "]}\n"
        "\n"
        "Rules:\n"
        "1. Only include targets that appear in the candidate list below.\n"
        "2. Cite the line where the call (not the definition) happens.\n"
        "3. evidence_snippet must be a substring of the file content at "
        "evidence_line (±2 lines tolerated).\n"
        '4. If you cannot resolve the site, return {"edges": []}. '
        "Do not guess.\n"
        "\n"
        "Candidate target nodes (graph ids):\n"
        f"{bulleted}\n"
    )


def _build_user_message(site: UnresolvedSite) -> Message:
    return Message(
        role="user",
        content=(
            f"File: {site.file}\n"
            f"Containing function id: {site.containing_node_id}\n"
            f"Dispatch site (line {site.line}): {site.snippet}\n"
            "\n"
            "Surrounding source:\n"
            "```python\n"
            f"{site.surrounding_code}\n"
            "```\n"
        ),
    )


def _coerce_payload_to_edges(
    payload: dict,
    site: UnresolvedSite,
    candidate_ids: set[str],
) -> list[Edge]:
    """Convert the parsed LLM JSON into validated :class:`Edge` objects.

    Drops entries that:
      * fail :class:`_LLMEdgePayload` schema validation;
      * cite a target outside ``candidate_ids`` (belt-and-braces — the
        unconditional ``validate_target`` in the pipeline would drop
        them too, but failing fast keeps the validator logs cleaner).
    """
    raw_edges = payload.get("edges")
    if not isinstance(raw_edges, list):
        return []
    out: list[Edge] = []
    for entry in raw_edges:
        if not isinstance(entry, dict):
            continue
        try:
            parsed = _LLMEdgePayload(**entry)
        except ValidationError:
            log.info(
                "graph_gap_fill_entry_dropped",
                reason="schema",
                site=site.containing_node_id,
            )
            continue
        if parsed.target_node_id not in candidate_ids:
            log.info(
                "graph_gap_fill_entry_dropped",
                reason="target_outside_candidates",
                site=site.containing_node_id,
                target=parsed.target_node_id,
            )
            continue
        out.append(
            Edge(
                source=site.containing_node_id,
                target=parsed.target_node_id,
                kind="calls",
                evidence=EdgeEvidence(
                    file=site.file,
                    line=parsed.evidence_line,
                    snippet=parsed.evidence_snippet,
                ),
                source_kind="llm",
            ),
        )
    return out


async def gap_fill_site(
    provider: LLMProvider,
    workspace_path: str,
    site: UnresolvedSite,
    candidate_nodes: list[Node],
) -> list[Edge]:
    """One-shot LLM call to resolve a single dispatch site.

    Returns a (possibly empty) list of :class:`Edge` instances with
    ``source_kind="llm"``. Citation/target validation happens in the
    pipeline — this function is content-only.

    Provider errors and unparseable LLM responses are caught and
    surface as an empty list. The caller's gap-fill ↘ agent-escape
    fallback then has the chance to try again.
    """
    # Cap candidate pool and capture id set up-front for the soft filter.
    bounded = candidate_nodes[:_CANDIDATE_CAP]
    candidate_ids = [n.id for n in bounded]
    id_set = set(candidate_ids)

    system = _build_system_prompt(site, candidate_ids)
    user = _build_user_message(site)

    try:
        payload = await complete_json(
            provider,
            messages=[user],
            system=system,
            max_tokens=_GAP_FILL_MAX_TOKENS,
            temperature=0.0,
        )
    except ValueError as e:
        log.info(
            "graph_gap_fill_unparseable",
            site=site.containing_node_id,
            error=str(e),
        )
        return []
    except Exception as e:
        # Network / provider errors must not blow up the pipeline. Log
        # at WARNING because this is a real anomaly.
        log.warning(
            "graph_gap_fill_provider_error",
            site=site.containing_node_id,
            error=str(e),
            error_type=e.__class__.__name__,
        )
        return []

    return _coerce_payload_to_edges(payload, site, id_set)


__all__ = ["gap_fill_site"]
