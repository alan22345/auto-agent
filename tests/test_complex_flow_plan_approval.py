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
``write_plan``, ``read_plan_approval``, and ``resume_after_plan_approval``
are the three primitives the orchestrator calls.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle import plan_approval
from agent.lifecycle.workspace_paths import (
    PLAN_APPROVAL_PATH,
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
    """``finalize_plan`` writes plan.md and transitions to AWAITING_PLAN_APPROVAL.

    The helper is the orchestrator-facing entry point: pass it the task_id,
    workspace, and plan text; it persists the artefact and moves the state
    machine without touching ``handle_planning`` internals.
    """

    transition_mock = AsyncMock()
    with patch.object(plan_approval, "transition_task", transition_mock):
        await plan_approval.finalize_plan(
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
# 3. Approval file with verdict="approved" resumes to CODING.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approved_verdict_resumes_to_coding(tmp_path: Path) -> None:
    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / PLAN_APPROVAL_PATH).write_text(
        json.dumps(
            {"schema_version": "1", "verdict": "approved", "comments": ""},
        )
    )

    transition_mock = AsyncMock()
    publish_mock = AsyncMock()
    with (
        patch.object(plan_approval, "transition_task", transition_mock),
        patch.object(plan_approval, "publish", publish_mock),
    ):
        resumed = await plan_approval.resume_after_plan_approval(
            task_id=11,
            workspace=str(tmp_path),
        )

    assert resumed is True, "approved should report a state advance"
    transition_mock.assert_awaited_once()
    args, _ = transition_mock.call_args
    assert args[0] == 11
    assert args[1] == "coding"


# ---------------------------------------------------------------------------
# 4. Rejected verdict transitions to BLOCKED with comments attached.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_verdict_transitions_to_blocked(tmp_path: Path) -> None:
    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / PLAN_APPROVAL_PATH).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "verdict": "rejected",
                "comments": "Please cover edge case X.",
            },
        )
    )

    transition_mock = AsyncMock()
    with patch.object(plan_approval, "transition_task", transition_mock):
        resumed = await plan_approval.resume_after_plan_approval(
            task_id=12,
            workspace=str(tmp_path),
        )

    assert resumed is True
    transition_mock.assert_awaited_once()
    args, _ = transition_mock.call_args
    assert args[0] == 12
    assert args[1] == "blocked"
    # The reject comments must be carried into the BLOCKED message so the
    # human sees why the gate flipped.
    assert "edge case X" in args[2]


# ---------------------------------------------------------------------------
# 5. Missing approval file ⇒ stays in AWAITING_PLAN_APPROVAL.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_approval_keeps_task_in_awaiting_plan_approval(
    tmp_path: Path,
) -> None:
    """No file ⇒ no transition. The orchestrator polls; missing == not yet."""

    transition_mock = AsyncMock()
    with patch.object(plan_approval, "transition_task", transition_mock):
        resumed = await plan_approval.resume_after_plan_approval(
            task_id=13,
            workspace=str(tmp_path),
        )

    assert resumed is False
    transition_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Malformed schema_version raises so a bad upload is loud (not silent
#    auto-resume).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_schema_version_raises(tmp_path: Path) -> None:
    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / PLAN_APPROVAL_PATH).write_text(
        json.dumps(
            {"schema_version": "999", "verdict": "approved", "comments": ""},
        )
    )

    with pytest.raises(ValueError):
        await plan_approval.resume_after_plan_approval(
            task_id=14,
            workspace=str(tmp_path),
        )


# ---------------------------------------------------------------------------
# 7. State machine accepts the new transitions.
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
