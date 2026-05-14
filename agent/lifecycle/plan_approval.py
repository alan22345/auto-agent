"""Plan-approval gate for the complex flow — ADR-015 §5 Phase 5.

The complex flow's plan is written to ``.auto-agent/plan.md`` and the
task parks at ``AWAITING_PLAN_APPROVAL`` until either:

- the human approves via the UI (non-freeform), or
- the standin agent writes ``.auto-agent/plan_approval.json`` (freeform
  — wired in Phase 10; this module exposes the file-reading primitive
  the standin will use).

For Phase 5 the standin is *not* fired — the gate just blocks. Tests
simulate approval by writing the file directly.

Three primitives:

- :func:`write_plan` — persist the plan text to ``.auto-agent/plan.md``.
- :func:`finalize_plan` — write + transition to AWAITING_PLAN_APPROVAL.
- :func:`resume_after_plan_approval` — read the verdict and transition
  the task to CODING (approved) or BLOCKED (rejected). No-op when the
  file is missing.
"""

from __future__ import annotations

import os
from typing import Any

from agent.lifecycle._orchestrator_api import transition_task
from agent.lifecycle.workspace_paths import (
    AUTO_AGENT_DIR,
    PLAN_APPROVAL_PATH,
    PLAN_PATH,
)
from agent.lifecycle.workspace_reader import read_gate_file
from shared.events import publish, task_plan_ready
from shared.logging import setup_logging

log = setup_logging("agent.lifecycle.plan_approval")


# ---------------------------------------------------------------------------
# Plan-side: write .auto-agent/plan.md.
# ---------------------------------------------------------------------------


def write_plan(workspace_root: str, plan_text: str) -> str:
    """Persist ``plan_text`` to ``.auto-agent/plan.md``, returning the abs path.

    Idempotent — overwrites if a previous plan was written.
    """

    os.makedirs(os.path.join(workspace_root, AUTO_AGENT_DIR), exist_ok=True)
    target = os.path.join(workspace_root, PLAN_PATH)
    with open(target, "w") as fh:
        fh.write(plan_text)
    return target


async def finalize_plan(
    *,
    task_id: int,
    workspace: str,
    plan_text: str,
    publish_event: bool = True,
) -> None:
    """Write the plan and transition to AWAITING_PLAN_APPROVAL.

    The orchestrator-facing entry point. ``publish_event`` toggles the
    ``task_plan_ready`` notification — tests turn it off so they don't
    need a Redis fixture.
    """

    write_plan(workspace, plan_text)
    await transition_task(
        task_id,
        "awaiting_plan_approval",
        "Plan written to .auto-agent/plan.md; awaiting approval",
    )
    if publish_event:
        try:
            await publish(task_plan_ready(task_id, plan=plan_text))
        except Exception:  # pragma: no cover — defensive
            log.warning("plan_approval.publish_failed", task_id=task_id)


# ---------------------------------------------------------------------------
# Approval-side: read .auto-agent/plan_approval.json and resume.
# ---------------------------------------------------------------------------


_VALID_VERDICTS = {"approved", "rejected"}


async def resume_after_plan_approval(
    *,
    task_id: int,
    workspace: str,
) -> bool:
    """Read ``plan_approval.json`` and transition the task accordingly.

    Returns:
      - ``True`` if a verdict was found and the state machine advanced.
      - ``False`` if the file is missing (the gate is still open). The
        orchestrator polls; missing == "not yet."

    Raises:
      ValueError: malformed JSON, missing ``verdict``, unknown verdict,
        or wrong ``schema_version``.
    """

    payload = read_gate_file(workspace, PLAN_APPROVAL_PATH, schema_version="1")
    if payload is None:
        return False
    if not isinstance(payload, dict):
        raise ValueError(f"{PLAN_APPROVAL_PATH} must be a JSON object")

    verdict = payload.get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise ValueError(
            f"{PLAN_APPROVAL_PATH} verdict must be one of {sorted(_VALID_VERDICTS)}; "
            f"got {verdict!r}"
        )

    comments = (payload.get("comments") or "").strip()

    if verdict == "approved":
        await transition_task(
            task_id,
            "coding",
            f"Plan approved{(': ' + comments[:200]) if comments else ''}",
        )
        return True

    # verdict == "rejected"
    await transition_task(
        task_id,
        "blocked",
        f"Plan rejected: {comments[:500] or '(no comments provided)'}",
    )
    return True


def read_plan_approval(workspace_root: str) -> dict[str, Any] | None:
    """Return the parsed approval payload, or None if not yet written."""

    payload = read_gate_file(workspace_root, PLAN_APPROVAL_PATH, schema_version="1")
    if isinstance(payload, dict):
        return payload
    return None
