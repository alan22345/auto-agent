"""Fixture 2 assertion — ADR-015 §2 / Phase 13.

For a complex_large task, the architect MUST write
``.auto-agent/design.md`` AND the task MUST transition to
``AWAITING_DESIGN_APPROVAL`` BEFORE any builder dispatch fires (any
``TRIO_EXECUTING`` transition that follows ``ARCHITECT_DESIGNING``).

The agent provider's promptfoo output is extended here with two
fields the orchestrator can populate during an integration-eval run:

  - ``auto_agent_files``: ``{path: bytes_or_text}`` snapshot of
    ``.auto-agent/`` after the task finished. Must contain
    ``design.md`` (and, eventually, ``plan_approval.json``).
  - ``state_transitions``: ordered list of
    ``{"from": "<state>", "to": "<state>"}`` rows from the task's
    transition log.

This assertion verifies:

1. ``design.md`` exists and is non-empty.
2. The transition log contains
   ``ARCHITECT_DESIGNING → AWAITING_DESIGN_APPROVAL`` BEFORE any
   ``TRIO_EXECUTING`` (= per-item builder dispatch).
3. No backlog item has a recorded builder spawn until after the
   approval transition (when ``backlog_dispatch_log`` is present).
"""

from __future__ import annotations

import json


def _index_of_transition(
    transitions: list[dict],
    *,
    from_state: str,
    to_state: str,
) -> int:
    """Return the 0-based index of the first matching transition, or -1."""
    for i, t in enumerate(transitions):
        if t.get("from") == from_state and t.get("to") == to_state:
            return i
    return -1


def _index_of_state(transitions: list[dict], state: str) -> int:
    """Return the 0-based index of the first transition that lands on
    ``state`` (``to`` field), or -1.
    """
    for i, t in enumerate(transitions):
        if t.get("to") == state:
            return i
    return -1


def get_assert(output: str, context: dict) -> dict:
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"pass": False, "score": 0.0, "reason": "Output is not valid JSON"}

    if isinstance(data, dict) and data.get("error"):
        return {"pass": False, "score": 0.0, "reason": f"Error: {data['error']}"}

    auto_files = data.get("auto_agent_files") or {}
    transitions = data.get("state_transitions") or []

    score = 0.0
    reasons: list[str] = []

    # Check 1 — design.md exists and is non-empty.
    design_text = auto_files.get(".auto-agent/design.md") or auto_files.get("design.md")
    if not design_text:
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                "`.auto-agent/design.md` is missing — the architect did not "
                "write the design artefact. ADR-015 §2."
            ),
        }
    if not isinstance(design_text, str) or not design_text.strip():
        return {
            "pass": False,
            "score": 0.0,
            "reason": "`.auto-agent/design.md` is empty.",
        }
    score += 0.4
    reasons.append("design.md present")

    # Check 2 — AWAITING_DESIGN_APPROVAL precedes TRIO_EXECUTING.
    approval_idx = _index_of_state(transitions, "AWAITING_DESIGN_APPROVAL")
    if approval_idx == -1:
        return {
            "pass": False,
            "score": round(score, 2),
            "reason": "Task never reached AWAITING_DESIGN_APPROVAL. ADR-015 §2.",
        }

    # The earliest dispatch transition is the first TRIO_EXECUTING that
    # follows ARCHITECT_BACKLOG_EMIT (or any TRIO_EXECUTING for short).
    dispatch_idx = _index_of_state(transitions, "TRIO_EXECUTING")
    if dispatch_idx != -1 and dispatch_idx < approval_idx:
        return {
            "pass": False,
            "score": round(score, 2),
            "reason": (
                "Builder dispatch (TRIO_EXECUTING at index "
                f"{dispatch_idx}) preceded the design-approval gate "
                f"(AWAITING_DESIGN_APPROVAL at index {approval_idx}). "
                "ADR-015 §2 — no dispatch before design approval."
            ),
        }
    score += 0.4
    reasons.append("AWAITING_DESIGN_APPROVAL precedes TRIO_EXECUTING")

    # Check 3 — backlog dispatch log (when present) must not record any
    # builder spawn before the approval transition.
    dispatch_log = data.get("backlog_dispatch_log") or []
    if dispatch_log:
        # Each dispatch row has ``spawned_at_transition_index``; the
        # approval transition must precede every one of them.
        early_dispatches = [
            row
            for row in dispatch_log
            if row.get("spawned_at_transition_index", 1 << 30) < approval_idx
        ]
        if early_dispatches:
            return {
                "pass": False,
                "score": round(score, 2),
                "reason": (
                    f"{len(early_dispatches)} builder(s) were dispatched "
                    f"before AWAITING_DESIGN_APPROVAL: "
                    f"{[r.get('item_id') for r in early_dispatches]}"
                ),
            }
        score += 0.2
        reasons.append("no builder dispatched before approval")
    else:
        # No dispatch log shipped — give partial credit since the
        # transition-order check already pinned the contract.
        score += 0.1
        reasons.append("no backlog_dispatch_log supplied (transition order satisfied)")

    return {
        "pass": True,
        "score": round(score, 2),
        "reason": "; ".join(reasons),
    }
