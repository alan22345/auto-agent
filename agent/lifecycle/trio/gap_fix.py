"""Architect gap-fix loop — ADR-015 §4 / Phase 7.

When the final reviewer returns ``gaps_found``, the orchestrator
**resumes** the architect's persisted Session (Phase 6 stored it on
``ArchitectAttempt.session_blob_path``) and asks it to close the gaps.
The architect's reply is a fresh ``decision.json`` via the
``submit-architect-decision`` skill — typically
``{"action": "dispatch_new", "payload": {"items": [...]}}`` — and the
orchestrator dispatches the new items through the normal builder →
heavy-review loop.

Bounds (ADR-015 §4): **3 gap-fix rounds**. A 4th round skips the agent
entirely and returns a ``blocked`` decision so the caller can park the
task (non-freeform) or hand off to the improvement-agent standin
(freeform — Phase 10 wires the standin).
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from sqlalchemy import select

from agent.lifecycle.trio.architect import (
    _load_parent_for_run,
    _prepare_parent_workspace,
    create_architect_agent,
)
from agent.lifecycle.workspace_paths import DECISION_PATH
from agent.lifecycle.workspace_reader import read_gate_file
from shared.database import async_session
from shared.models import ArchitectAttempt

log = structlog.get_logger()


# Maximum gap-fix rounds before BLOCKED — ADR-015 §4.
MAX_GAP_FIX_ROUNDS = 3


async def _load_architect_session(parent_task_id: int):
    """Load the architect's most-recent persisted Session for this parent.

    Walks ``ArchitectAttempt`` rows newest-first for the parent and
    returns the first ``Session`` whose blob exists on disk. Returns
    ``None`` if no resumable session is available (caller falls back to a
    fresh architect run).
    """

    from agent.session import Session

    async with async_session() as s:
        rows = (
            (
                await s.execute(
                    select(ArchitectAttempt)
                    .where(ArchitectAttempt.task_id == parent_task_id)
                    .where(ArchitectAttempt.session_blob_path.is_not(None))
                    .order_by(ArchitectAttempt.id.desc())
                    .limit(5)
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        return None

    # ArchitectAttempt.session_blob_path is workspace-relative
    # (Phase 6 saves it as ``trio-<id>.json``). The actual ``Session``
    # load happens with the workspace path available, inside
    # ``run_gap_fix`` — we just signal "resumable session exists" here.
    session_id = f"trio-{parent_task_id}"
    return Session(session_id=session_id, storage_dir="<placeholder>")


def _render_gaps(gaps: list[dict]) -> str:
    if not gaps:
        return "(no gaps)"
    lines: list[str] = []
    for g in gaps:
        lines.append(f"- {g.get('description', '')} (routes: {g.get('affected_routes') or []!r})")
    return "\n".join(lines)


_GAP_FIX_PROMPT = """\
The final reviewer found the following gaps after the per-item loop
drained. Resume your architect session and decide how to close them.

You MUST use the ``submit-architect-decision`` skill to write
``.auto-agent/decision.json``. The preferred action is
``"dispatch_new"`` with new backlog items in the payload. If you
believe the gaps can't be closed without escalation, use
``"escalate"`` instead.

This is gap-fix round {round_idx} of {max_rounds}. After {max_rounds}
rounds the orchestrator blocks the task automatically.

== Final reviewer's gaps ==
{gaps}
"""


async def run_gap_fix(
    *,
    parent_task_id: int,
    gaps: list[dict[str, Any]],
    round_idx: int,
) -> dict[str, Any]:
    """Resume the architect with the gap list; return its decision.

    Bound check fires first — round_idx > MAX_GAP_FIX_ROUNDS returns
    ``{"action": "blocked", "reason": "gap_fix_round_limit"}`` without
    invoking any agent.

    Returns the decision dict read from ``.auto-agent/decision.json``.
    On dispatch_new the decision's ``items`` (lifted from
    ``payload.items``) are returned in a top-level ``items`` key for
    caller convenience.
    """

    if round_idx > MAX_GAP_FIX_ROUNDS:
        log.info(
            "trio.gap_fix.round_limit_reached",
            parent_id=parent_task_id,
            round_idx=round_idx,
        )
        return {
            "action": "blocked",
            "reason": "gap_fix_round_limit",
            "rounds_exhausted": MAX_GAP_FIX_ROUNDS,
        }

    fields = await _load_parent_for_run(parent_task_id)
    workspace = await _prepare_parent_workspace(fields.get("__parent"))
    workspace_root = workspace.root if hasattr(workspace, "root") else str(workspace)

    session = await _load_architect_session(parent_task_id)
    if session is not None and getattr(session, "storage_dir", None) == "<placeholder>":
        # The loader returned a placeholder; rebind storage to the real
        # workspace path now that we have it.
        from agent.session import Session

        session = Session(
            session_id=f"trio-{parent_task_id}",
            storage_dir=workspace_root,
        )

    agent = create_architect_agent(
        workspace=workspace,
        task_id=parent_task_id,
        task_description=fields["task_description"],
        phase="checkpoint",
        repo_name=fields["repo_name"],
        home_dir=fields["home_dir"],
        org_id=fields["org_id"],
        session=session,
    )

    prompt = _GAP_FIX_PROMPT.format(
        round_idx=round_idx,
        max_rounds=MAX_GAP_FIX_ROUNDS,
        gaps=_render_gaps(gaps),
    )

    # Clear stale decision.json so we don't pick up a prior round's verdict.
    decision_abs = os.path.join(workspace_root, DECISION_PATH)
    if os.path.isfile(decision_abs):
        os.remove(decision_abs)

    await agent.run(prompt, resume=session is not None)

    payload = read_gate_file(workspace_root, DECISION_PATH, schema_version="1")
    if not isinstance(payload, dict):
        log.warning(
            "trio.gap_fix.missing_decision",
            parent_id=parent_task_id,
            round_idx=round_idx,
        )
        return {
            "action": "blocked",
            "reason": "architect did not write decision.json on gap-fix turn",
        }

    decision = dict(payload)
    inner_items = (
        decision.get("payload", {}).get("items")
        if isinstance(decision.get("payload"), dict)
        else None
    )
    if isinstance(inner_items, list):
        decision["items"] = list(inner_items)

    log.info(
        "trio.gap_fix.decision",
        parent_id=parent_task_id,
        round_idx=round_idx,
        action=decision.get("action"),
        item_count=len(decision.get("items") or []),
    )
    return decision


__all__ = ["MAX_GAP_FIX_ROUNDS", "run_gap_fix"]
