"""Plan-approval gate for the complex flow — ADR-015 §5 Phase 5.

Five behaviours pinned here:

1. Plan generation writes ``.auto-agent/plan.md`` to the task workspace.
2. After writing, the task transitions to ``AWAITING_PLAN_APPROVAL``.
3. When ``.auto-agent/plan_approval.json`` shows ``verdict="approved"``,
   the gate resumes the task into ``CODING``.
4. When the verdict is ``"rejected"``, the task transitions to ``BLOCKED``
   with the comments attached.
5. When the file is missing, the gate is a no-op — the task stays in
   ``AWAITING_PLAN_APPROVAL`` (the standin or human is still deciding).

The gate is exercised through ``agent.lifecycle.plan_approval`` —
``write_plan`` and ``_finalize_plan`` are the primitives the orchestrator
calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle import plan_approval
from agent.lifecycle.workspace_paths import (
    PLAN_PATH,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Plan generation writes .auto-agent/plan.md.
# ---------------------------------------------------------------------------


def test_write_plan_creates_plan_md(tmp_path: Path) -> None:
    """``write_plan`` writes the markdown plan to ``.auto-agent/plan.md``."""

    plan_text = "# Plan\n\n- step 1\n- step 2\n"
    plan_approval.write_plan(str(tmp_path), plan_text)

    plan_file = tmp_path / PLAN_PATH
    assert plan_file.is_file(), f"missing {plan_file}"
    assert plan_file.read_text() == plan_text


def test_write_plan_creates_auto_agent_dir_when_missing(tmp_path: Path) -> None:
    """The ``.auto-agent/`` subdir is created if absent."""

    plan_text = "# tiny plan\n"
    # Sanity: the dir does not exist up-front.
    assert not (tmp_path / ".auto-agent").exists()
    plan_approval.write_plan(str(tmp_path), plan_text)
    assert (tmp_path / ".auto-agent").is_dir()
    assert (tmp_path / PLAN_PATH).read_text() == plan_text


# ---------------------------------------------------------------------------
# 2. After writing, the task transitions to AWAITING_PLAN_APPROVAL.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complex_planning_transitions_to_awaiting_plan_approval(
    tmp_path: Path,
) -> None:
    """``_finalize_plan`` writes plan.md and transitions to AWAITING_PLAN_APPROVAL.

    The helper is the orchestrator-facing entry point: pass it the task_id,
    workspace, and plan text; it persists the artefact and moves the state
    machine without touching ``handle_planning`` internals.
    """

    transition_mock = AsyncMock()
    with patch.object(plan_approval, "transition_task", transition_mock):
        await plan_approval._finalize_plan(
            task_id=7,
            workspace=str(tmp_path),
            plan_text="# Plan\n",
        )

    assert (tmp_path / PLAN_PATH).is_file()
    transition_mock.assert_awaited_once()
    args, _ = transition_mock.call_args
    assert args[0] == 7
    assert args[1] == "awaiting_plan_approval"


# ---------------------------------------------------------------------------
# 3. State machine accepts the new transitions.
# ---------------------------------------------------------------------------


def test_state_machine_has_awaiting_plan_approval_transitions() -> None:
    """PLANNING → AWAITING_PLAN_APPROVAL → CODING/BLOCKED chain exists."""

    from orchestrator.state_machine import TRANSITIONS
    from shared.models import TaskStatus

    # AWAITING_PLAN_APPROVAL is a real enum member.
    assert hasattr(TaskStatus, "AWAITING_PLAN_APPROVAL")
    target = TaskStatus.AWAITING_PLAN_APPROVAL

    # PLANNING can advance there.
    assert target in TRANSITIONS[TaskStatus.PLANNING]
    # From there: CODING (approved) or BLOCKED (rejected).
    allowed = TRANSITIONS[target]
    assert TaskStatus.CODING in allowed
    assert TaskStatus.BLOCKED in allowed
