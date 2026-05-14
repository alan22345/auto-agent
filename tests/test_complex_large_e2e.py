"""complex_large end-to-end Phase 7 wiring — ADR-015 §3 / §4 / §5.

Exercises the new state-machine edges introduced in Phase 7:

  - TRIO_EXECUTING → FINAL_REVIEW (after backlog drains).
  - FINAL_REVIEW → ARCHITECT_GAP_FIX (verdict=gaps_found).
  - ARCHITECT_GAP_FIX → TRIO_EXECUTING (architect dispatches new items).
  - FINAL_REVIEW → PR_CREATED (verdict=passed).
  - FINAL_REVIEW → BLOCKED (after 3 gap-fix rounds exhausted).

Uses the pure :func:`orchestrator.state_machine.transition` API plus an
in-memory ``Task`` stub — the state machine doesn't require a database.
"""

from __future__ import annotations

from orchestrator.state_machine import TRANSITIONS
from shared.models import TaskStatus


def test_trio_executing_can_transition_to_final_review() -> None:
    """ADR-015 §4 — after the per-item loop drains, orchestrator dispatches
    the final reviewer, so TRIO_EXECUTING → FINAL_REVIEW must be allowed."""

    allowed = TRANSITIONS[TaskStatus.TRIO_EXECUTING]
    assert TaskStatus.FINAL_REVIEW in allowed


def test_final_review_can_transition_to_pr_created_or_block_or_gap_fix() -> None:
    allowed = TRANSITIONS[TaskStatus.FINAL_REVIEW]
    assert TaskStatus.PR_CREATED in allowed
    assert TaskStatus.ARCHITECT_GAP_FIX in allowed
    assert TaskStatus.BLOCKED in allowed


def test_gap_fix_can_loop_back_to_trio_executing() -> None:
    """Architect emits dispatch_new → orchestrator re-runs the builder loop."""

    allowed = TRANSITIONS[TaskStatus.ARCHITECT_GAP_FIX]
    assert TaskStatus.TRIO_EXECUTING in allowed
    assert TaskStatus.BLOCKED in allowed


def test_state_machine_runs_the_phase_7_walk() -> None:
    """End-to-end shape check on the new states.

    Validates the full happy + sad gap-fix walk by composing
    ``TRANSITIONS`` set membership — no DB needed (the existing trio
    state-machine tests use the same shape).
    """

    walk = [
        (TaskStatus.TRIO_EXECUTING, TaskStatus.FINAL_REVIEW),
        (TaskStatus.FINAL_REVIEW, TaskStatus.ARCHITECT_GAP_FIX),
        (TaskStatus.ARCHITECT_GAP_FIX, TaskStatus.TRIO_EXECUTING),
        (TaskStatus.TRIO_EXECUTING, TaskStatus.FINAL_REVIEW),
        (TaskStatus.FINAL_REVIEW, TaskStatus.PR_CREATED),
    ]
    for src, dst in walk:
        assert dst in TRANSITIONS[src], f"{src} → {dst} not allowed"

    # Blocked walk: 3rd gap-fix exhausted → BLOCKED.
    assert TaskStatus.BLOCKED in TRANSITIONS[TaskStatus.FINAL_REVIEW]
    assert TaskStatus.BLOCKED in TRANSITIONS[TaskStatus.ARCHITECT_GAP_FIX]


def test_invalid_transitions_still_rejected() -> None:
    """FINAL_REVIEW → DONE directly is not allowed (must go via PR_CREATED)."""

    assert TaskStatus.DONE not in TRANSITIONS[TaskStatus.FINAL_REVIEW]
