"""State machine transitions — Phase 4 additions for BLOCKED_ON_QUOTA + explicit BLOCKED_ON_AUTH exits."""

from orchestrator.state_machine import TRANSITIONS
from shared.models import TaskStatus


def test_blocked_on_quota_can_be_entered_from_active_states() -> None:
    for src in (TaskStatus.PLANNING, TaskStatus.CODING, TaskStatus.QUEUED):
        assert TaskStatus.BLOCKED_ON_QUOTA in TRANSITIONS[src], (
            f"{src.value} should be able to transition to BLOCKED_ON_QUOTA"
        )


def test_blocked_on_quota_exits_to_queued() -> None:
    allowed = TRANSITIONS[TaskStatus.BLOCKED_ON_QUOTA]
    assert TaskStatus.QUEUED in allowed
    assert TaskStatus.FAILED in allowed


def test_blocked_on_auth_exits_defined() -> None:
    # Phase 4 also defines BLOCKED_ON_AUTH exits (previously implicit).
    allowed = TRANSITIONS[TaskStatus.BLOCKED_ON_AUTH]
    assert TaskStatus.QUEUED in allowed


# ---------------------------------------------------------------------------
# Freeform self-verification — VERIFYING state (Task 2)
# ---------------------------------------------------------------------------


def test_coding_to_verifying() -> None:
    assert TaskStatus.VERIFYING in TRANSITIONS[TaskStatus.CODING], (
        "CODING should be able to transition to VERIFYING"
    )


def test_verifying_to_pr_created() -> None:
    assert TaskStatus.PR_CREATED in TRANSITIONS[TaskStatus.VERIFYING], (
        "VERIFYING should be able to transition to PR_CREATED"
    )


def test_verifying_back_to_coding() -> None:
    assert TaskStatus.CODING in TRANSITIONS[TaskStatus.VERIFYING], (
        "VERIFYING should be able to transition back to CODING (verify failed)"
    )


def test_verifying_to_blocked() -> None:
    assert TaskStatus.BLOCKED in TRANSITIONS[TaskStatus.VERIFYING], (
        "VERIFYING should be able to transition to BLOCKED (cycle 2 failure)"
    )


def test_awaiting_review_to_blocked() -> None:
    assert TaskStatus.BLOCKED in TRANSITIONS[TaskStatus.AWAITING_REVIEW], (
        "AWAITING_REVIEW should be able to transition to BLOCKED (review failed cycle 2)"
    )
