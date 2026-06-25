"""Plan-approval gate for the complex flow — ADR-015 §5 Phase 5.

The complex flow's plan is written to ``.auto-agent/plan.md`` and the
task parks at ``AWAITING_PLAN_APPROVAL`` until either:

- the human approves via the UI (non-freeform), or
- the standin agent writes ``.auto-agent/plan_approval.json`` (freeform
  — wired in Phase 10; this module exposes the file-reading primitive
  the standin will use).

For Phase 5 the standin is *not* fired — the gate just blocks. Tests
simulate approval by writing the file directly.

Two primitives:

- :func:`write_plan` — persist the plan text to ``.auto-agent/plan.md``.
- :func:`_finalize_plan` — write + transition to AWAITING_PLAN_APPROVAL.
"""

from __future__ import annotations

import os

from agent.lifecycle._orchestrator_api import transition_task
from agent.lifecycle.workspace_paths import (
    AUTO_AGENT_DIR,
    PLAN_PATH,
)
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


async def _finalize_plan(
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
