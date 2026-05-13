"""Verifies TRANSITIONS allows the trio paths and rejects illegal ones."""
from shared.models import TaskStatus
from orchestrator.state_machine import TRANSITIONS


def test_queued_can_enter_trio():
    assert TaskStatus.TRIO_EXECUTING in TRANSITIONS[TaskStatus.QUEUED]


def test_trio_executing_to_pr_or_blocked():
    allowed = TRANSITIONS[TaskStatus.TRIO_EXECUTING]
    assert TaskStatus.PR_CREATED in allowed
    assert TaskStatus.BLOCKED in allowed
    assert TaskStatus.DONE not in allowed


def test_verifying_to_trio_review():
    assert TaskStatus.TRIO_REVIEW in TRANSITIONS[TaskStatus.VERIFYING]


def test_trio_review_to_pr_or_back_to_coding():
    allowed = TRANSITIONS[TaskStatus.TRIO_REVIEW]
    assert TaskStatus.PR_CREATED in allowed
    assert TaskStatus.CODING in allowed
    assert TaskStatus.BLOCKED in allowed


def test_coding_can_enter_trio_review():
    assert TaskStatus.TRIO_REVIEW in TRANSITIONS[TaskStatus.CODING]


def test_awaiting_ci_can_re_enter_trio_executing():
    assert TaskStatus.TRIO_EXECUTING in TRANSITIONS[TaskStatus.AWAITING_CI]


def test_trio_executing_can_transition_to_awaiting_clarification():
    assert TaskStatus.AWAITING_CLARIFICATION in TRANSITIONS[TaskStatus.TRIO_EXECUTING]


def test_awaiting_clarification_can_transition_to_trio_executing():
    assert TaskStatus.TRIO_EXECUTING in TRANSITIONS[TaskStatus.AWAITING_CLARIFICATION]


def test_existing_transitions_unchanged():
    """Sanity: planner's clarification path is untouched."""
    assert TaskStatus.PLANNING in TRANSITIONS[TaskStatus.AWAITING_CLARIFICATION]
    assert TaskStatus.CODING in TRANSITIONS[TaskStatus.AWAITING_CLARIFICATION]
    assert TaskStatus.AWAITING_CLARIFICATION in TRANSITIONS[TaskStatus.PLANNING]
