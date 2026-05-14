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


# ---------------------------------------------------------------------------
# Self-PR-review (ADR-015 §5) — PR_REVIEW state, Phase 4
# ---------------------------------------------------------------------------


def test_pr_review_status_exists() -> None:
    """The PR_REVIEW enum member must exist so the state machine can refer to it."""

    assert hasattr(TaskStatus, "PR_REVIEW"), (
        "TaskStatus.PR_REVIEW must exist for the self-PR-review gate (ADR-015 §5)"
    )


def test_pr_created_to_pr_review() -> None:
    """After PR is opened, the orchestrator hands off to the PR reviewer."""

    assert TaskStatus.PR_REVIEW in TRANSITIONS[TaskStatus.PR_CREATED], (
        "PR_CREATED should be able to transition to PR_REVIEW (ADR-015 §5)"
    )


def test_pr_review_to_done() -> None:
    """A passing PR review on the simple flow terminates the task."""

    assert TaskStatus.DONE in TRANSITIONS[TaskStatus.PR_REVIEW], (
        "PR_REVIEW should be able to transition to DONE on pass"
    )


def test_pr_review_to_blocked() -> None:
    """A failing PR review surfaces the issue for human visibility."""

    assert TaskStatus.BLOCKED in TRANSITIONS[TaskStatus.PR_REVIEW], (
        "PR_REVIEW should be able to transition to BLOCKED on fail"
    )


def test_pr_review_rejects_unspecified_targets() -> None:
    """Make sure invalid transitions still reject (regression for §5 wiring)."""

    allowed = TRANSITIONS[TaskStatus.PR_REVIEW]
    # PR_REVIEW should NOT loop back to e.g. PLANNING; the simple flow has no
    # planning step. (It can re-enter CODING via BLOCKED → CODING.)
    assert TaskStatus.PLANNING not in allowed
    assert TaskStatus.AWAITING_APPROVAL not in allowed
