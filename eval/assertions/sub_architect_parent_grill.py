"""Fixture 3 assertion — ADR-015 §10 / Phase 13.

When the architect spawns sub-architects, every grill question a sub-
architect emits MUST be answered by the parent architect — never by the
user, the PO standin, or the improvement-agent standin.

The agent provider's promptfoo output is extended with:

  - ``grill_rounds``: ordered list of
    ``{"slice": <name>, "question": <text>, "answerer": <agent_id>,
       "answerer_source": "parent_architect" | "user" | "po_standin" |
       "improvement_standin"}``.
  - ``gate_decisions``: optional snapshot of ``gate_decisions`` rows
    whose ``gate == "grill"``; for each parent-relay round the source
    column must NEVER be ``po_standin`` or ``improvement_standin``.

Pass criteria:

1. ``grill_rounds`` contains at least one entry (the fixture is built
   to force this; if zero rounds were observed, the architect didn't
   actually spawn sub-architects on this task).
2. Every grill round's ``answerer_source`` is ``"parent_architect"``.
3. If ``gate_decisions`` is present, every grill row has
   ``source == "parent_architect"`` (or omits the source field — the
   parent-relay path doesn't write a GateDecision row in the current
   migration because parent-relays are not human-equivalent gates).

Fail conditions surface the offending slice + answerer so the failure
is debuggable from the assertion message alone.
"""

from __future__ import annotations

import json

_STANDIN_SOURCES = {"po_standin", "improvement_standin", "user"}


def get_assert(output: str, context: dict) -> dict:
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"pass": False, "score": 0.0, "reason": "Output is not valid JSON"}

    if isinstance(data, dict) and data.get("error"):
        return {"pass": False, "score": 0.0, "reason": f"Error: {data['error']}"}

    grill_rounds = data.get("grill_rounds") or []
    gate_decisions = data.get("gate_decisions") or []

    if not grill_rounds:
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                "No grill rounds observed. The fixture expects at least one "
                "sub-architect grill question to be relayed to the parent."
            ),
        }

    # The parent-answers-grill rule.
    offenders = [r for r in grill_rounds if r.get("answerer_source") != "parent_architect"]
    if offenders:
        first = offenders[0]
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                f"Sub-architect grill was answered by "
                f"{first.get('answerer_source')!r} "
                f"(slice={first.get('slice')!r}, "
                f"answerer={first.get('answerer')!r}). ADR-015 §10 — only "
                "the parent architect may answer."
            ),
        }

    # gate_decisions cross-check: no standin row may exist for any
    # grill round on this task.
    standin_grill_rows = [
        row
        for row in gate_decisions
        if row.get("gate") == "grill" and row.get("source") in _STANDIN_SOURCES
    ]
    if standin_grill_rows:
        offender = standin_grill_rows[0]
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                "gate_decisions row shows a standin "
                f"({offender.get('source')!r}) answering a grill — ADR-015 "
                "§10 forbids this when the question came from a sub-"
                "architect. Use the parent-relay path."
            ),
        }

    score = 0.5 + min(0.5, 0.1 * len(grill_rounds))
    return {
        "pass": True,
        "score": round(score, 2),
        "reason": (
            f"{len(grill_rounds)} grill round(s) — every one answered by the "
            "parent architect. ADR-015 §10 honoured."
        ),
    }
