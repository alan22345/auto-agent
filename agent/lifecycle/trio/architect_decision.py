"""Architect per-cycle decision — ADR-015 §2 / §9 / §12 / Phase 6.

The trio architect emits its per-cycle decision via the
``submit-architect-decision`` skill, which writes ``.auto-agent/decision.json``
to the workspace. The orchestrator reads the file after ``agent.run``
returns.

If the file is missing, the orchestrator falls back to the ADR-014
Haiku extractor on the prose output. The fallback exists so we keep the
old resilience net during the transition window — the new skill path is
primary, the extractor is the backstop.

Five valid actions:

- ``done`` — every backlog item shipped + final review passes. ``payload`` may be ``{}``.
- ``dispatch_new`` — append fresh backlog items to close gaps. ``payload``: ``{"items": [...]}``.
- ``escalate`` — auto-agent cannot close the loop on its own. ``payload``: ``{"reason": "..."}``.
- ``spawn_sub_architects`` — slice the task across sub-architects.
  ``payload``: ``{"slices": [{"name": "...", "scope": "..."}, ...]}``.
- ``awaiting_clarification`` — a question blocks progress. ``payload``: ``{"question": "..."}``.

The orchestrator just READS this in Phase 6; the dispatching of
sub-architects + the per-item dispatcher reshape is Phase 7+.
"""

from __future__ import annotations

from typing import Any

from agent.lifecycle.trio.extract import extract_checkpoint_output
from agent.lifecycle.workspace_paths import DECISION_PATH
from agent.lifecycle.workspace_reader import read_gate_file

_VALID_ACTIONS: tuple[str, ...] = (
    "done",
    "dispatch_new",
    "escalate",
    "spawn_sub_architects",
    "awaiting_clarification",
)


def _validate_payload(action: str, payload: Any) -> bool:
    """Cheap structural validation of the action-specific payload shape.

    Returns ``False`` on any obviously broken payload; the orchestrator
    treats that as if the decision was never written and falls through
    to the Haiku-extractor fallback.
    """

    if not isinstance(payload, dict):
        return False

    if action == "done":
        return True

    if action == "dispatch_new":
        items = payload.get("items")
        return isinstance(items, list) and bool(items)

    if action == "escalate":
        reason = payload.get("reason")
        return isinstance(reason, str) and bool(reason.strip())

    if action == "spawn_sub_architects":
        slices = payload.get("slices")
        if not isinstance(slices, list) or not slices:
            return False
        for s in slices:
            if not isinstance(s, dict):
                return False
            name = s.get("name")
            scope = s.get("scope")
            if not isinstance(name, str) or not name.strip():
                return False
            if not isinstance(scope, str) or not scope.strip():
                return False
        return True

    if action == "awaiting_clarification":
        question = payload.get("question")
        return isinstance(question, str) and bool(question.strip())

    return False


def read_decision(workspace_root: str) -> dict[str, Any] | None:
    """Read and validate ``.auto-agent/decision.json``.

    Returns ``None`` when the file is missing OR when the action is
    unknown OR when the payload shape is broken for the action.
    """

    payload = read_gate_file(
        workspace_root,
        DECISION_PATH,
        schema_version="1",
    )
    if payload is None:
        return None
    if not isinstance(payload, dict):
        return None

    action = payload.get("action")
    if action not in _VALID_ACTIONS:
        return None

    body = payload.get("payload", {})
    if not _validate_payload(action, body):
        return None

    return {"action": action, "payload": body}


async def resolve_decision(
    *,
    workspace: str,
    prose_output: str,
) -> dict[str, Any] | None:
    """Read decision.json if present; else fall back to the Haiku extractor.

    Returns:
        - The decision dict (``{"action": str, "payload": dict}``) when
          the file is present + valid.
        - The Haiku-extracted decision when the file is missing — the
          ADR-014 extractor returns ``{"decision": {...}, "backlog": ...}``;
          we project to the ``{"action", "payload"}`` shape so callers
          have one shape to branch on.
        - ``None`` when both paths fail.
    """

    decision = read_decision(workspace)
    if decision is not None:
        return decision

    # Fallback: Haiku extractor on the prose output.
    extracted = await extract_checkpoint_output(prose_output)
    if not extracted or "decision" not in extracted:
        return None

    raw = extracted["decision"]
    action = str(raw.get("action", "")).strip().lower()
    if not action:
        return None

    # Normalise legacy action names from ADR-014 onto the §9 vocabulary.
    legacy_map = {
        "continue": "done",
        "revise": "dispatch_new",
        "blocked": "escalate",
    }
    action = legacy_map.get(action, action)
    if action not in _VALID_ACTIONS:
        return None

    body: dict[str, Any] = {}
    if action == "awaiting_clarification":
        question = str(raw.get("question", "")).strip()
        if not question:
            return None
        body["question"] = question
    elif action == "escalate":
        body["reason"] = raw.get("reason", "extractor fallback")

    return {"action": action, "payload": body}


__all__ = [
    "read_decision",
    "resolve_decision",
]
