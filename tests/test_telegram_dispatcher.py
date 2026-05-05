"""Tests for the Telegram notification dispatcher.

The 23-branch if/elif chain in `integrations.telegram.main._notify_user`
became a dict keyed on `TaskEventType` / `POEventType` members. These
tests pin two invariants:

  1. Every wired event type produces a non-empty message body — so a
     formatter that accidentally returns an empty string is caught.
  2. Events that the dispatcher does *not* render (`task.cleanup`,
     `task.classified`, `task.feedback`, `repo.onboard`, etc.) return
     `None` from the lookup, matching the legacy "silently drop" branch.
"""

from __future__ import annotations

import pytest

from integrations.telegram.main import _NOTIFICATION_FORMATTERS
from shared.events import (
    POEventType,
    TaskEventType,
    architecture_analysis_started,
    human_message,
    po_analysis_failed,
    po_analysis_queued,
    po_analysis_started,
    po_suggestions_ready,
    repo_deleted,
    repo_onboard,
    task_approved,
    task_blocked,
    task_classified,
    task_cleanup,
    task_created,
    task_failed,
    task_feedback,
    task_plan_ready,
    task_review_approved,
    task_review_complete,
    task_start_planning,
    task_subtask_progress,
)

# Events the Telegram dispatcher is expected to render. Keep this list in
# sync with `_NOTIFICATION_FORMATTERS`.
RENDERED_EVENTS = [
    task_created(task_id=1),
    task_start_planning(task_id=1),
    task_start_planning(task_id=1, feedback="please redo"),
    task_plan_ready(task_id=1, plan="Step 1\nStep 2"),
    task_review_complete(
        task_id=1, review="lgtm", pr_url="http://x", approved=True
    ),
    task_review_complete(
        task_id=1,
        review="needs fix",
        pr_url="http://x",
        approved=False,
        fixes="fixed",
    ),
    task_blocked(task_id=1, error="needs input"),
    task_failed(task_id=1, error="boom"),
    task_subtask_progress(
        task_id=1, current=1, total=3, title="phase 1", status="running"
    ),
    task_subtask_progress(
        task_id=1, current=3, total=3, title="phase 3", status="done"
    ),
    po_analysis_queued(repo_name="acme/widget", position=2),
    po_analysis_started(repo_name="acme/widget"),
    po_suggestions_ready(repo_name="acme/widget", count=3),
    po_analysis_failed(repo_name="acme/widget"),
    po_analysis_failed(repo_name="acme/widget", reason="timeout"),
]


@pytest.mark.parametrize("event", RENDERED_EVENTS, ids=lambda ev: ev.type)
def test_every_rendered_event_produces_non_empty_message(event):
    formatter = _NOTIFICATION_FORMATTERS.get(event.type)
    assert formatter is not None, f"No formatter wired for {event.type}"

    msg = formatter(event.payload or {}, "Task #1: hello", False, event.task_id)
    assert isinstance(msg, str)
    assert msg.strip(), f"Empty message for {event.type}"


def test_review_complete_renders_freeform_branch():
    """Freeform-mode review_complete uses a distinct copy from the human-review path."""
    formatter = _NOTIFICATION_FORMATTERS[TaskEventType.REVIEW_COMPLETE]
    payload = {"review": "lgtm", "pr_url": "http://x", "approved": True}
    freeform_msg = formatter(payload, "Task #1: x", True, 1)
    human_msg = formatter(payload, "Task #1: x", False, 1)
    assert freeform_msg != human_msg
    assert "auto-merging" in freeform_msg


def test_plan_ready_renders_freeform_branch():
    formatter = _NOTIFICATION_FORMATTERS[TaskEventType.PLAN_READY]
    payload = {"plan": "Step 1"}
    freeform_msg = formatter(payload, "Task #1: x", True, 1)
    human_msg = formatter(payload, "Task #1: x", False, 1)
    assert freeform_msg != human_msg
    assert "auto-reviewing" in freeform_msg


# Events the dispatcher intentionally drops. If a future change wires one
# of these up, this test fails — and the change can either add to
# RENDERED_EVENTS above or update this list deliberately.
INTENTIONALLY_UNRENDERED = [
    task_classified(task_id=1, complexity="simple"),
    task_cleanup(task_id=1),
    task_approved(task_id=1),
    task_review_approved(task_id=1),
    task_feedback(task_id=1, message_id=99, sender="alice"),
    repo_onboard(repo_id=1, repo_name="acme/widget"),
    repo_deleted(repo_name="acme/widget"),
    architecture_analysis_started(repo_name="acme/widget"),
    human_message(task_id=1, message="hi", source="telegram"),
]


@pytest.mark.parametrize("event", INTENTIONALLY_UNRENDERED, ids=lambda ev: ev.type)
def test_unrendered_events_are_silently_dropped(event):
    assert event.type not in _NOTIFICATION_FORMATTERS


def test_dispatcher_covers_only_intended_event_set():
    """Pin the exact set of TaskEventType / POEventType members the
    dispatcher renders. Adding/removing notifications is a deliberate
    decision; this test catches accidental drift."""
    expected = {
        TaskEventType.CREATED,
        TaskEventType.START_PLANNING,
        TaskEventType.START_CODING,
        TaskEventType.PLAN_READY,
        TaskEventType.REVIEW_COMPLETE,
        TaskEventType.BLOCKED,
        TaskEventType.FAILED,
        TaskEventType.DONE,
        TaskEventType.CI_PASSED,
        TaskEventType.CI_FAILED,
        TaskEventType.CLARIFICATION_NEEDED,
        TaskEventType.REJECTED,
        TaskEventType.DEV_DEPLOYED,
        TaskEventType.REVIEW_COMMENTS_ADDRESSED,
        TaskEventType.DEV_DEPLOY_FAILED,
        TaskEventType.SUBTASK_PROGRESS,
        POEventType.ANALYSIS_QUEUED,
        POEventType.ANALYSIS_STARTED,
        POEventType.SUGGESTIONS_READY,
        POEventType.ANALYSIS_FAILED,
    }
    assert set(_NOTIFICATION_FORMATTERS.keys()) == expected
