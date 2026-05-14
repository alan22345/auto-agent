"""Cheap-model structured extractors for trio decision points.

The architect (and reviewer) produce long prose responses. Asking the
same model to also emit a JSON envelope at the end of that prose has
proven brittle — the model spends its turn on the reasoning and forgets
the structural commitment (task 170 on 2026-05-14 was the latest
incident; architect.checkpoint produced 6 KB of markdown review with no
JSON block and the parent ended up BLOCKED).

This module splits the contract: the big model reasons in prose, then
each prose response is passed through a Haiku call here whose ONLY job
is to extract the structured envelope. Two small models, each doing
one job, instead of one big model doing two.

All extractors:

- Use ``get_structured_extractor_provider()`` (Bedrock + Haiku) — the
  cheap classifier path is independent of ``LLM_PROVIDER``, so it works
  whether the heavy turn ran via Bedrock or via ``claude_cli`` pass-through.
- Route through ``agent.llm.structured.complete_json`` which handles
  fence-stripping + brace-locating + one bounded retry with a "your last
  reply wasn't valid JSON" nudge.
- Return narrow ``TypedDict``-shaped ``dict`` results that the caller
  branches on. ``None`` only when ``complete_json`` exhausts its retries
  (caller decides whether to mark BLOCKED or use a legacy regex fallback).
"""

from __future__ import annotations

import structlog

from agent.llm import get_structured_extractor_provider
from agent.llm.structured import complete_json
from agent.llm.types import Message

log = structlog.get_logger()


# Hard cap on prose length we ship to the extractor. The architect's
# output can be 10s of KB; Haiku is fine with that but burning context
# on the tail of an 80 KB analysis is wasteful. The decision is always
# near the end of the response, so we keep the last N characters.
_MAX_INPUT_CHARS = 24_000


def _trim_for_extractor(text: str) -> str:
    if not text:
        return ""
    if len(text) <= _MAX_INPUT_CHARS:
        return text
    return "... (earlier output truncated)\n\n" + text[-_MAX_INPUT_CHARS:]


# ---------------------------------------------------------------------------
# Architect — run_initial output: backlog OR clarification.
# ---------------------------------------------------------------------------


_INITIAL_SYSTEM = """\
You extract the structured decision from a software architect's "initial \
pass" output. The architect either produced a backlog of work items \
(the normal path) OR asked for a human clarification (the rare blocking \
path). Your job is to read their prose and return JSON of the form:

{"kind": "backlog", "items": [{"id": "T1", "title": "...", "description": "..."}, ...]}

or

{"kind": "clarification", "question": "..."}

Rules:
- If the architect committed to a list of work items, return kind=backlog.
- If the architect explicitly asked the human a blocking question, return
  kind=clarification, with the question text. Pack multi-part questions
  into one string as a numbered markdown list.
- The architect's prose is authoritative. Do not invent items they didn't
  describe. Do not invent a clarification they didn't ask for.
- Item ids: prefer ones the architect named (e.g. "T1", "cf-1"). If they
  didn't name them, use sequential "T1", "T2", ....
- Item descriptions: include ENOUGH context that a builder who has not
  read the architect's full reasoning can do the work. Don't truncate to
  a sentence if the architect went into depth.
- Output JSON only — no prose, no markdown fences.
"""


async def extract_initial_output(text: str) -> dict | None:
    """Extract the architect's run_initial decision from prose output.

    Returns ``{"kind": "backlog", "items": [...]}`` or
    ``{"kind": "clarification", "question": "..."}`` or ``None`` on
    extractor failure.
    """
    try:
        data = await complete_json(
            get_structured_extractor_provider(),
            messages=[Message(role="user", content=_trim_for_extractor(text))],
            system=_INITIAL_SYSTEM,
            max_tokens=2000,
            retries=2,
        )
    except Exception as exc:
        log.warning(
            "trio.extract.initial_failed",
            error=str(exc),
            preview=(text or "")[:200],
        )
        return None

    kind = str(data.get("kind", "")).lower()
    if kind == "backlog":
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return None
        normalised: list[dict] = []
        for i, raw in enumerate(items):
            if not isinstance(raw, dict):
                continue
            normalised.append({
                "id": str(raw.get("id", "")) or f"T{i + 1}",
                "title": str(raw.get("title", "")),
                "description": str(raw.get("description", "")),
                "status": "pending",
            })
        if not normalised:
            return None
        return {"kind": "backlog", "items": normalised}

    if kind == "clarification":
        q = str(data.get("question", "")).strip()
        if not q:
            return None
        return {"kind": "clarification", "question": q}

    return None


# ---------------------------------------------------------------------------
# Architect — checkpoint output: decision + optional backlog amendment.
# ---------------------------------------------------------------------------


_CHECKPOINT_SYSTEM = """\
You extract the architect's checkpoint decision from their review output. \
The architect ran a checkpoint pass over an integration branch and \
decided ONE of:

- "done" — everything in the backlog is complete and the integration is
  sound; open the integration PR.
- "continue" — keep going; next pending item should be dispatched.
- "revise" — design needs change; re-enter architecting phase.
- "blocked" — cannot proceed.
- "awaiting_clarification" — needs human input; question must be supplied.

Return JSON of the form:

{"decision": {"action": "done|continue|revise|blocked|awaiting_clarification",
              "reason": "<one short sentence>",
              "question": "<only if awaiting_clarification>"},
 "backlog": null | [{"id": "...", "title": "...", "description": "..."}, ...]}

Rules:
- Choose the action by reading the architect's review prose. If they
  said "ready to ship" or "we're done", that's "done". If they listed
  missing pieces or rejected the work, that's "revise" or "blocked"
  (or "awaiting_clarification" if they asked the human a question).
- "backlog" is only non-null when the architect explicitly listed NEW or
  REVISED work items. If they only commented on the existing items
  without rewriting them, return null and the orchestrator will keep
  the parent's existing backlog.
- Output JSON only — no prose, no markdown fences.
"""


async def extract_checkpoint_output(text: str) -> dict | None:
    """Extract a checkpoint decision (and optional amended backlog).

    Returns ``{"decision": {...}, "backlog": list | None}`` or ``None``.
    """
    try:
        data = await complete_json(
            get_structured_extractor_provider(),
            messages=[Message(role="user", content=_trim_for_extractor(text))],
            system=_CHECKPOINT_SYSTEM,
            max_tokens=2000,
            retries=2,
        )
    except Exception as exc:
        log.warning(
            "trio.extract.checkpoint_failed",
            error=str(exc),
            preview=(text or "")[:200],
        )
        return None

    decision = data.get("decision")
    if not isinstance(decision, dict):
        return None
    action = str(decision.get("action", "")).strip().lower()
    valid = {"done", "continue", "revise", "blocked", "awaiting_clarification"}
    if action not in valid:
        return None

    out_decision: dict = {"action": action}
    reason = str(decision.get("reason", "")).strip()
    if reason:
        out_decision["reason"] = reason
    question = str(decision.get("question", "")).strip()
    if action == "awaiting_clarification" and not question:
        # Required for this action.
        return None
    if question:
        out_decision["question"] = question

    backlog_raw = data.get("backlog")
    backlog: list[dict] | None = None
    if isinstance(backlog_raw, list) and backlog_raw:
        backlog = []
        for i, raw in enumerate(backlog_raw):
            if not isinstance(raw, dict):
                continue
            backlog.append({
                "id": str(raw.get("id", "")) or f"T{i + 1}",
                "title": str(raw.get("title", "")),
                "description": str(raw.get("description", "")),
                "status": str(raw.get("status", "pending")),
            })
        if not backlog:
            backlog = None

    return {"decision": out_decision, "backlog": backlog}


# ---------------------------------------------------------------------------
# Dispatcher — reviewer verdict: {ok, feedback}.
# ---------------------------------------------------------------------------


_REVIEW_SYSTEM = """\
You extract a code reviewer's verdict from their review prose. The \
reviewer judged whether the builder's diff satisfies a work item. \
Return JSON of the form:

{"ok": true|false, "feedback": "<actionable, specific>"}

Rules:
- "ok" is true ONLY when the reviewer explicitly approved (no remaining
  issues, ready to proceed). Default to false if ambiguous.
- "feedback" captures the reviewer's concrete concerns or the affirming
  statement, paraphrased tightly. Empty string is fine when ok=true and
  the reviewer simply said "looks good".
- The reviewer's prose is authoritative. Don't add concerns they didn't
  raise. Don't downgrade approval to mild praise.
- Output JSON only — no prose, no markdown fences.
"""


async def extract_review_verdict(text: str) -> dict | None:
    """Extract a reviewer verdict from prose output.

    Returns ``{"ok": bool, "feedback": str}`` or ``None``.
    """
    try:
        data = await complete_json(
            get_structured_extractor_provider(),
            messages=[Message(role="user", content=_trim_for_extractor(text))],
            system=_REVIEW_SYSTEM,
            max_tokens=800,
            retries=2,
        )
    except Exception as exc:
        log.warning(
            "trio.extract.review_failed",
            error=str(exc),
            preview=(text or "")[:200],
        )
        return None

    if "ok" not in data:
        return None
    return {
        "ok": bool(data.get("ok")),
        "feedback": str(data.get("feedback", "")).strip(),
    }


# ---------------------------------------------------------------------------
# Dispatcher — architect tiebreak: structured decision.
# ---------------------------------------------------------------------------


_TIEBREAK_SYSTEM = """\
You extract the architect's tiebreak decision from their review of a \
coder↔reviewer disagreement. The architect chose ONE of:

- "accept" — coder is right; mark this work item done.
- "redo" — reviewer is right; supply specific guidance for the next coder run.
- "revise_backlog" — the work item itself is wrong; supply replacement items.
- "clarify" — needs human input; supply the question.

Return JSON of the form:

{"action": "accept|redo|revise_backlog|clarify",
 "reason": "<one short sentence>",
 "guidance": "<only for redo>",
 "new_items": [{"id": "...", "title": "...", "description": "..."}, ...],   # only for revise_backlog
 "question": "<only for clarify>"}

Rules:
- Output JSON only — no prose, no markdown fences.
- "guidance", "new_items", "question" are only populated for the
  matching action; leave the others out.
"""


async def extract_tiebreak_decision(text: str) -> dict | None:
    """Extract a tiebreak decision from architect prose."""
    try:
        data = await complete_json(
            get_structured_extractor_provider(),
            messages=[Message(role="user", content=_trim_for_extractor(text))],
            system=_TIEBREAK_SYSTEM,
            max_tokens=1500,
            retries=2,
        )
    except Exception as exc:
        log.warning(
            "trio.extract.tiebreak_failed",
            error=str(exc),
            preview=(text or "")[:200],
        )
        return None

    action = str(data.get("action", "")).strip().lower()
    valid = {"accept", "redo", "revise_backlog", "clarify"}
    if action not in valid:
        return None

    out: dict = {"action": action}
    reason = str(data.get("reason", "")).strip()
    if reason:
        out["reason"] = reason

    if action == "redo":
        guidance = str(data.get("guidance", "")).strip()
        if guidance:
            out["guidance"] = guidance
    elif action == "revise_backlog":
        new_items_raw = data.get("new_items")
        if not isinstance(new_items_raw, list) or not new_items_raw:
            return None
        new_items: list[dict] = []
        for i, raw in enumerate(new_items_raw):
            if not isinstance(raw, dict):
                continue
            new_items.append({
                "id": str(raw.get("id", "")) or f"T{i + 1}",
                "title": str(raw.get("title", "")),
                "description": str(raw.get("description", "")),
            })
        if not new_items:
            return None
        out["new_items"] = new_items
    elif action == "clarify":
        question = str(data.get("question", "")).strip()
        if not question:
            return None
        out["question"] = question

    return out


__all__ = [
    "extract_checkpoint_output",
    "extract_initial_output",
    "extract_review_verdict",
    "extract_tiebreak_decision",
]
