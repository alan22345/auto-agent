"""Fixture 4 assertion — ADR-015 §6 + Phase 12 / Phase 13.

In freeform mode the standin (PO by default, improvement-agent when the
task came from a deepening suggestion) MUST:

1. Write ``.auto-agent/grill_answer.json`` (the canonical gate file).
2. Publish a ``standin.decision`` Redis event with the full payload
   schema (``standin_kind``, ``agent_id``, ``gate``, ``decision``,
   ``cited_context``, ``fallback_reasons``, ``timestamp``).
3. Persist a ``gate_decisions`` row with the matching audit fields
   (``source``, ``agent_id``, ``gate``, ``verdict``, ``comments``,
   ``cited_context``, ``fallback_reasons``).

The promptfoo agent_provider output is extended with three blobs:

  - ``grill_answer_file``: parsed ``.auto-agent/grill_answer.json``.
  - ``standin_event``: the published ``standin.decision`` event payload.
  - ``gate_decision_row``: the persisted ``GateDecision`` row as a dict
    (mirrors the Phase-12 schema in ``shared/models/core.py``).

Pass criteria:

A. All three blobs are present.
B. ``standin_event.standin_kind`` is one of ``po``/``improvement_agent``.
C. ``gate_decision_row.source`` is one of
   ``po_standin``/``improvement_standin`` and matches the event's
   ``standin_kind`` modulo the ``_standin`` suffix.
D. ``gate_decision_row.verdict`` and ``standin_event.decision`` agree
   with ``grill_answer_file.answer`` (the durable audit must reflect
   the same decision the gate file records).
E. Required audit fields are present and structurally well-formed.
"""

from __future__ import annotations

import json

_VALID_SOURCES = {"po_standin", "improvement_standin"}
_VALID_STANDIN_KINDS = {"po", "improvement_agent"}
_REQUIRED_AUDIT_FIELDS = (
    "task_id",
    "gate",
    "source",
    "agent_id",
    "verdict",
    "cited_context",
    "fallback_reasons",
)


def _missing_fields(row: dict, fields: tuple[str, ...]) -> list[str]:
    return [f for f in fields if f not in row]


def _source_matches_kind(source: str, kind: str) -> bool:
    """Map ``po``↔``po_standin`` and ``improvement_agent``↔``improvement_standin``."""
    return (source == "po_standin" and kind == "po") or (
        source == "improvement_standin" and kind == "improvement_agent"
    )


def get_assert(output: str, context: dict) -> dict:
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"pass": False, "score": 0.0, "reason": "Output is not valid JSON"}

    if isinstance(data, dict) and data.get("error"):
        return {"pass": False, "score": 0.0, "reason": f"Error: {data['error']}"}

    answer_file = data.get("grill_answer_file") or {}
    event = data.get("standin_event") or {}
    row = data.get("gate_decision_row") or {}

    if not answer_file:
        return {
            "pass": False,
            "score": 0.0,
            "reason": "`.auto-agent/grill_answer.json` not in agent output.",
        }
    if not event:
        return {
            "pass": False,
            "score": 0.0,
            "reason": "No `standin.decision` event payload in agent output.",
        }
    if not row:
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                "No `gate_decisions` row persisted. ADR-015 §6 / Phase 12 "
                "requires every freeform standin gate to write a durable "
                "audit row alongside the ephemeral Redis event."
            ),
        }

    # Required audit fields on the DB row.
    missing = _missing_fields(row, _REQUIRED_AUDIT_FIELDS)
    if missing:
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                f"`gate_decisions` row missing required field(s): {missing}. "
                "ADR-015 §6 audit schema."
            ),
        }

    # Source / kind taxonomy.
    source = row.get("source")
    kind = event.get("standin_kind")
    if source not in _VALID_SOURCES:
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                f"`gate_decisions.source` must be one of {sorted(_VALID_SOURCES)}; got {source!r}."
            ),
        }
    if kind not in _VALID_STANDIN_KINDS:
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                f"`standin_event.standin_kind` must be one of "
                f"{sorted(_VALID_STANDIN_KINDS)}; got {kind!r}."
            ),
        }
    if not _source_matches_kind(source, kind):
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                f"Source/kind disagreement: source={source!r} but "
                f"standin_kind={kind!r}. Audit row and event must agree."
            ),
        }

    # Decision agreement across the three sinks.
    file_answer = (answer_file.get("answer") or "").strip()
    event_decision = (event.get("decision") or "").strip()
    row_verdict = (row.get("verdict") or "").strip()
    # Grill gate: ``decision`` is the literal "answered" verb in the
    # event taxonomy; the answer text itself lives in the file. The
    # audit row's verdict should carry the same answer text.
    if event.get("gate") == "grill" or row.get("gate") == "grill":
        # Event decision is "answered" for grill — accept that as a
        # marker. The file + row carry the answer.
        if event_decision and event_decision.lower() not in {"answered", file_answer.lower()}:
            return {
                "pass": False,
                "score": 0.0,
                "reason": (
                    f"Event.decision={event_decision!r} disagrees with "
                    f"grill_answer.answer={file_answer!r}."
                ),
            }
        if (
            row_verdict
            and file_answer
            and row_verdict.lower()
            not in {
                "answered",
                file_answer.lower(),
            }
        ):
            return {
                "pass": False,
                "score": 0.0,
                "reason": (
                    "Audit-row verdict and grill_answer disagree: "
                    f"row.verdict={row_verdict!r} vs file.answer={file_answer!r}."
                ),
            }
    else:
        # Non-grill gate: row.verdict must match event.decision.
        if event_decision and row_verdict and event_decision != row_verdict:
            return {
                "pass": False,
                "score": 0.0,
                "reason": (
                    f"Event.decision={event_decision!r} disagrees with row.verdict={row_verdict!r}."
                ),
            }

    # cited_context shape: must be a list, possibly empty.
    cited = row.get("cited_context")
    if not isinstance(cited, list) or not all(isinstance(s, str) for s in cited):
        return {
            "pass": False,
            "score": 0.0,
            "reason": "`gate_decisions.cited_context` must be list[str].",
        }

    # fallback_reasons shape: must be a list, possibly empty.
    fbr = row.get("fallback_reasons")
    if not isinstance(fbr, list) or not all(isinstance(s, str) for s in fbr):
        return {
            "pass": False,
            "score": 0.0,
            "reason": "`gate_decisions.fallback_reasons` must be list[str].",
        }

    return {
        "pass": True,
        "score": 1.0,
        "reason": (
            f"Standin gate audit complete: source={source}, gate={row.get('gate')}, "
            f"verdict={row_verdict or '(see file)'} — event + DB row + gate file "
            "agree (ADR-015 §6 + Phase 12)."
        ),
    }
