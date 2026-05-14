"""Design-doc approval gate for complex_large — ADR-015 §2 / Phase 6.

The architect's first turn writes ``.auto-agent/design.md`` via the
``submit-design`` skill; the parent task transitions to
``AWAITING_DESIGN_APPROVAL`` and waits for ``.auto-agent/plan_approval.json``
(reusing the complex-flow approval contract — design and plan share the
mechanism; the file's ``verdict`` field is what matters).

Approved → ``ARCHITECT_BACKLOG_EMIT``; rejected → ``BLOCKED`` with the
comments attached. Missing approval file ⇒ no transition — the
orchestrator polls.

Three primitives:

- :func:`write_design` — persist the design text to ``.auto-agent/design.md``.
- :func:`finalize_design` — write + transition to AWAITING_DESIGN_APPROVAL.
- :func:`resume_after_design_approval` — read the verdict and advance the
  parent task to ARCHITECT_BACKLOG_EMIT (approved) or BLOCKED (rejected).
"""

from __future__ import annotations

import os
from typing import Any

from agent.lifecycle._orchestrator_api import transition_task
from agent.lifecycle.workspace_paths import (
    AUTO_AGENT_DIR,
    DESIGN_PATH,
    PLAN_APPROVAL_PATH,
)
from agent.lifecycle.workspace_reader import read_gate_file
from shared.logging import setup_logging

log = setup_logging("agent.lifecycle.trio.design_approval")


# ---------------------------------------------------------------------------
# Design-side: write .auto-agent/design.md.
# ---------------------------------------------------------------------------


def write_design(workspace_root: str, design_text: str) -> str:
    """Persist ``design_text`` to ``.auto-agent/design.md``.

    Idempotent — overwrites if a previous design was written. Returns
    the absolute path so callers can log it.
    """

    os.makedirs(os.path.join(workspace_root, AUTO_AGENT_DIR), exist_ok=True)
    target = os.path.join(workspace_root, DESIGN_PATH)
    with open(target, "w") as fh:
        fh.write(design_text)
    return target


async def finalize_design(
    *,
    task_id: int,
    workspace: str,
    design_text: str | None = None,
) -> None:
    """Write the design (if provided) and transition to AWAITING_DESIGN_APPROVAL.

    The orchestrator-facing entry point. ``design_text`` is optional —
    the agent itself may have written ``design.md`` via the
    ``submit-design`` skill before this is called; passing ``None``
    just transitions the task without rewriting the file.
    """

    if design_text is not None:
        write_design(workspace, design_text)
    await transition_task(
        task_id,
        "awaiting_design_approval",
        "Design written to .auto-agent/design.md; awaiting approval",
    )


# ---------------------------------------------------------------------------
# Approval-side: read .auto-agent/plan_approval.json and advance.
# ---------------------------------------------------------------------------


_VALID_VERDICTS = {"approved", "rejected"}


async def resume_after_design_approval(
    *,
    task_id: int,
    workspace: str,
) -> bool:
    """Read ``plan_approval.json`` and transition the parent task.

    Returns:
      - ``True`` when a verdict was found and the state machine advanced.
      - ``False`` when the file is missing (the gate is still open).
    Raises:
      ValueError: malformed JSON, missing ``verdict``, unknown verdict,
        or wrong ``schema_version``.
    """

    payload: Any = read_gate_file(
        workspace,
        PLAN_APPROVAL_PATH,
        schema_version="1",
    )
    if payload is None:
        return False
    if not isinstance(payload, dict):
        raise ValueError(f"{PLAN_APPROVAL_PATH} must be a JSON object")

    verdict = payload.get("verdict")
    if verdict not in _VALID_VERDICTS:
        raise ValueError(
            f"{PLAN_APPROVAL_PATH} verdict must be one of "
            f"{sorted(_VALID_VERDICTS)}; got {verdict!r}"
        )

    comments = (payload.get("comments") or "").strip()

    if verdict == "approved":
        await transition_task(
            task_id,
            "architect_backlog_emit",
            f"Design approved{(': ' + comments[:200]) if comments else ''}",
        )
        return True

    # verdict == "rejected"
    await transition_task(
        task_id,
        "blocked",
        f"Design rejected: {comments[:500] or '(no comments provided)'}",
    )
    return True


def read_design_approval(workspace_root: str) -> dict[str, Any] | None:
    """Return the parsed approval payload, or None if not yet written."""

    payload = read_gate_file(
        workspace_root,
        PLAN_APPROVAL_PATH,
        schema_version="1",
    )
    if isinstance(payload, dict):
        return payload
    return None


__all__ = [
    "finalize_design",
    "read_design_approval",
    "resume_after_design_approval",
    "write_design",
]
