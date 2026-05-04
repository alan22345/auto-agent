"""Tests for the typed event taxonomy in shared/events.py.

These tests pin the wire strings of every enum member (so renaming a
producer literal can't silently desynchronise from a consumer that hasn't
been migrated) and check that every factory produces the expected
``Event`` shape.
"""

from __future__ import annotations

import pytest

from shared.events import (
    ArchitectureEventType,
    Event,
    HumanEventType,
    POEventType,
    RepoEventType,
    TaskEventType,
    architecture_analysis_failed,
    architecture_analysis_started,
    architecture_suggestions_ready,
    human_message,
    po_analysis_failed,
    po_analysis_queued,
    po_analysis_started,
    po_analyze,
    po_suggestions_ready,
    repo_deleted,
    repo_onboard,
    task_approved,
    task_blocked,
    task_ci_failed,
    task_ci_passed,
    task_clarification_needed,
    task_clarification_resolved,
    task_clarification_response,
    task_classified,
    task_cleanup,
    task_created,
    task_deploy_preview,
    task_dev_deploy_failed,
    task_dev_deployed,
    task_done,
    task_failed,
    task_feedback,
    task_plan_ready,
    task_query,
    task_rejected,
    task_review_approved,
    task_review_comments_addressed,
    task_review_complete,
    task_start_coding,
    task_start_planning,
    task_start_queued,
    task_status_changed,
    task_subtask_progress,
)

# ---------------------------------------------------------------------------
# Wire-string pinning — each enum member's value matches the legacy literal.
# A producer using TaskEventType.X must produce the same bytes on the wire as
# Event(type="task.x") did before the migration; consumers comparing
# event.type == "task.x" must keep matching.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "member,expected",
    [
        (TaskEventType.CREATED, "task.created"),
        (TaskEventType.CLASSIFIED, "task.classified"),
        (TaskEventType.STATUS_CHANGED, "task.status_changed"),
        (TaskEventType.QUERY, "task.query"),
        (TaskEventType.START_PLANNING, "task.start_planning"),
        (TaskEventType.START_CODING, "task.start_coding"),
        (TaskEventType.PLAN_READY, "task.plan_ready"),
        (TaskEventType.DEPLOY_PREVIEW, "task.deploy_preview"),
        (TaskEventType.CI_PASSED, "task.ci_passed"),
        (TaskEventType.CI_FAILED, "task.ci_failed"),
        (TaskEventType.REVIEW_COMPLETE, "task.review_complete"),
        (TaskEventType.REVIEW_APPROVED, "task.review_approved"),
        (TaskEventType.REVIEW_COMMENTS_ADDRESSED, "task.review_comments_addressed"),
        (TaskEventType.APPROVED, "task.approved"),
        (TaskEventType.REJECTED, "task.rejected"),
        (TaskEventType.CLARIFICATION_NEEDED, "task.clarification_needed"),
        (TaskEventType.CLARIFICATION_RESPONSE, "task.clarification_response"),
        (TaskEventType.CLARIFICATION_RESOLVED, "task.clarification_resolved"),
        (TaskEventType.BLOCKED, "task.blocked"),
        (TaskEventType.FAILED, "task.failed"),
        (TaskEventType.CLEANUP, "task.cleanup"),
        (TaskEventType.DEV_DEPLOYED, "task.dev_deployed"),
        (TaskEventType.DEV_DEPLOY_FAILED, "task.dev_deploy_failed"),
        (TaskEventType.FEEDBACK, "task.feedback"),
        (TaskEventType.DONE, "task.done"),
        (TaskEventType.START_QUEUED, "task.start_queued"),
        (TaskEventType.SUBTASK_PROGRESS, "task.subtask_progress"),
        (POEventType.ANALYZE, "po.analyze"),
        (POEventType.ANALYSIS_QUEUED, "po.analysis_queued"),
        (POEventType.ANALYSIS_STARTED, "po.analysis_started"),
        (POEventType.ANALYSIS_FAILED, "po.analysis_failed"),
        (POEventType.SUGGESTIONS_READY, "po.suggestions_ready"),
        (ArchitectureEventType.ANALYSIS_STARTED, "architecture.analysis_started"),
        (ArchitectureEventType.ANALYSIS_FAILED, "architecture.analysis_failed"),
        (ArchitectureEventType.SUGGESTIONS_READY, "architecture.suggestions_ready"),
        (RepoEventType.ONBOARD, "repo.onboard"),
        (RepoEventType.DELETED, "repo.deleted"),
        (HumanEventType.MESSAGE, "human.message"),
    ],
)
def test_enum_member_value_matches_legacy_string(member, expected):
    assert member == expected
    assert member.value == expected


def test_strenum_compares_equal_to_plain_string():
    """A consumer that does ``event.type == "task.created"`` must keep working
    after producers switch to the enum — StrEnum is a str subclass, so equality
    holds on the raw string value."""
    ev = task_created(task_id=1)
    assert ev.type == "task.created"
    assert ev.type == TaskEventType.CREATED


# ---------------------------------------------------------------------------
# Factory-output shape — each factory produces an Event with the right type
# and payload keys. A typo in a factory key would silently route a payload
# the wrong way; pin every key explicitly.
# ---------------------------------------------------------------------------


def test_task_created():
    ev = task_created(task_id=1)
    assert ev.type == TaskEventType.CREATED
    assert ev.task_id == 1
    assert ev.payload == {}


def test_task_classified_passes_through_kwargs():
    ev = task_classified(task_id=1, complexity="simple", reasoning="trivial")
    assert ev.type == TaskEventType.CLASSIFIED
    assert ev.payload == {"complexity": "simple", "reasoning": "trivial"}


def test_task_status_changed():
    ev = task_status_changed(task_id=1, status="planning", message="hi")
    assert ev.type == TaskEventType.STATUS_CHANGED
    assert ev.payload == {"status": "planning", "message": "hi"}


def test_task_query():
    ev = task_query(task_id=2)
    assert ev.type == TaskEventType.QUERY
    assert ev.task_id == 2
    assert ev.payload == {}


def test_task_start_planning_without_feedback():
    ev = task_start_planning(task_id=3)
    assert ev.type == TaskEventType.START_PLANNING
    assert ev.payload == {}


def test_task_start_planning_with_feedback():
    ev = task_start_planning(task_id=3, feedback="please redo")
    assert ev.payload == {"feedback": "please redo"}


def test_task_start_coding_without_retry():
    ev = task_start_coding(task_id=4)
    assert ev.type == TaskEventType.START_CODING
    assert ev.payload == {}


def test_task_start_coding_with_retry_reason():
    ev = task_start_coding(task_id=4, retry_reason="ci broke")
    assert ev.payload == {"retry_reason": "ci broke"}


def test_task_plan_ready():
    ev = task_plan_ready(task_id=5, plan="do X")
    assert ev.type == TaskEventType.PLAN_READY
    assert ev.payload == {"plan": "do X"}


def test_task_deploy_preview():
    ev = task_deploy_preview(task_id=6)
    assert ev.type == TaskEventType.DEPLOY_PREVIEW
    assert ev.payload == {}


def test_task_ci_passed():
    ev = task_ci_passed(task_id=7)
    assert ev.type == TaskEventType.CI_PASSED
    assert ev.payload == {}


def test_task_ci_failed_requires_reason():
    ev = task_ci_failed(task_id=7, reason="lint failed")
    assert ev.type == TaskEventType.CI_FAILED
    assert ev.payload == {"reason": "lint failed"}


def test_task_review_complete_minimal():
    ev = task_review_complete(task_id=8, review="lgtm", pr_url="http://x", approved=True)
    assert ev.type == TaskEventType.REVIEW_COMPLETE
    assert ev.payload == {"review": "lgtm", "pr_url": "http://x", "approved": True}


def test_task_review_complete_with_optionals():
    ev = task_review_complete(
        task_id=8,
        review="needs fix",
        pr_url="http://x",
        approved=False,
        branch="auto-agent/foo",
        fixes="fixed it",
    )
    assert ev.payload["branch"] == "auto-agent/foo"
    assert ev.payload["fixes"] == "fixed it"


def test_task_review_approved():
    ev = task_review_approved(task_id=9)
    assert ev.type == TaskEventType.REVIEW_APPROVED


def test_task_review_comments_addressed():
    ev = task_review_comments_addressed(task_id=10, output="diff", pr_url="http://x")
    assert ev.payload == {"output": "diff", "pr_url": "http://x"}


def test_task_approved():
    ev = task_approved(task_id=11)
    assert ev.type == TaskEventType.APPROVED


def test_task_rejected_requires_feedback():
    ev = task_rejected(task_id=11, feedback="no")
    assert ev.payload == {"feedback": "no"}


def test_task_clarification_needed():
    ev = task_clarification_needed(task_id=12, question="what?")
    assert ev.payload == {"question": "what?"}
    ev2 = task_clarification_needed(task_id=12, question="what?", phase="planning")
    assert ev2.payload == {"question": "what?", "phase": "planning"}


def test_task_clarification_response():
    ev = task_clarification_response(task_id=12, answer="42")
    assert ev.payload == {"answer": "42"}


def test_task_clarification_resolved():
    assert task_clarification_resolved(task_id=12).payload == {}
    assert task_clarification_resolved(task_id=12, output="done").payload == {"output": "done"}


def test_task_blocked_optional_error():
    assert task_blocked(task_id=13).payload == {}
    assert task_blocked(task_id=13, error="oops").payload == {"error": "oops"}


def test_task_failed_optional_error():
    assert task_failed(task_id=14).payload == {}
    assert task_failed(task_id=14, error="boom").payload == {"error": "boom"}


def test_task_cleanup():
    ev = task_cleanup(task_id=15)
    assert ev.type == TaskEventType.CLEANUP


def test_task_dev_deployed():
    ev = task_dev_deployed(task_id=16, branch="auto-agent/x", output="ok", pr_url="http://x")
    assert ev.payload == {"branch": "auto-agent/x", "output": "ok", "pr_url": "http://x"}


def test_task_dev_deploy_failed():
    ev = task_dev_deploy_failed(task_id=16, branch="auto-agent/x", output="boom", pr_url="http://x")
    assert ev.type == TaskEventType.DEV_DEPLOY_FAILED
    assert ev.payload == {"branch": "auto-agent/x", "output": "boom", "pr_url": "http://x"}


def test_task_feedback():
    ev = task_feedback(task_id=17, message_id=99, sender="alice")
    assert ev.payload == {"message_id": 99, "sender": "alice"}


def test_task_done():
    ev = task_done(task_id=18)
    assert ev.type == TaskEventType.DONE


def test_task_start_queued():
    ev = task_start_queued(task_id=19)
    assert ev.type == TaskEventType.START_QUEUED


def test_task_subtask_progress():
    ev = task_subtask_progress(task_id=20, current=1, total=3, title="phase 1", status="running")
    assert ev.payload == {"current": 1, "total": 3, "title": "phase 1", "status": "running"}


def test_po_analyze():
    ev = po_analyze(repo_id=42, repo_name="acme/widget")
    assert ev.task_id == 0
    assert ev.payload == {"repo_id": 42, "repo_name": "acme/widget"}


def test_po_analysis_queued():
    ev = po_analysis_queued(repo_name="acme/widget", position=2)
    assert ev.payload == {"repo_name": "acme/widget", "position": 2}


def test_po_analysis_started():
    ev = po_analysis_started(repo_name="acme/widget")
    assert ev.payload == {"repo_name": "acme/widget"}


def test_po_analysis_failed():
    ev = po_analysis_failed(repo_name="acme/widget")
    assert ev.payload == {"repo_name": "acme/widget"}
    ev2 = po_analysis_failed(repo_name="acme/widget", reason="timeout")
    assert ev2.payload == {"repo_name": "acme/widget", "reason": "timeout"}


def test_po_suggestions_ready():
    ev = po_suggestions_ready(repo_name="acme/widget", count=3)
    assert ev.payload == {"repo_name": "acme/widget", "count": 3}


def test_architecture_analysis_started():
    ev = architecture_analysis_started(repo_name="acme/widget")
    assert ev.type == ArchitectureEventType.ANALYSIS_STARTED
    assert ev.payload == {"repo_name": "acme/widget"}


def test_architecture_analysis_failed():
    ev = architecture_analysis_failed(repo_name="acme/widget", reason="parse")
    assert ev.payload == {"repo_name": "acme/widget", "reason": "parse"}


def test_architecture_suggestions_ready():
    ev = architecture_suggestions_ready(repo_name="acme/widget", count=2)
    assert ev.payload == {"repo_name": "acme/widget", "count": 2}


def test_repo_onboard():
    ev = repo_onboard(repo_id=1, repo_name="acme/widget")
    assert ev.task_id == 0
    assert ev.payload == {"repo_id": 1, "repo_name": "acme/widget"}


def test_repo_deleted():
    ev = repo_deleted(repo_name="acme/widget")
    assert ev.payload == {"repo_name": "acme/widget"}


def test_human_message():
    ev = human_message(task_id=21, message="hi", source="telegram")
    assert ev.type == HumanEventType.MESSAGE
    assert ev.payload == {"message": "hi", "source": "telegram"}


# ---------------------------------------------------------------------------
# Wire round-trip — an Event constructed via factory must serialise to the
# same wire shape that consumers comparing against the literal string have
# always seen, and ``from_redis`` must reconstruct an equivalent event.
# ---------------------------------------------------------------------------


def test_to_redis_serialises_strenum_to_legacy_string():
    ev = task_created(task_id=42)
    wire = ev.to_redis()
    assert wire["type"] == "task.created"
    # The JSON-encoded `data` blob also carries the bare string, not an
    # enum-prefixed form — old consumers reading the stream see exactly what
    # they used to.
    assert '"type":"task.created"' in wire["data"].replace(" ", "")


def test_round_trip_through_from_redis():
    original = task_ci_failed(task_id=42, reason="lint")
    wire = original.to_redis()
    decoded = Event.from_redis({"data": wire["data"]})
    assert decoded.type == "task.ci_failed"
    assert decoded.task_id == 42
    assert decoded.payload == {"reason": "lint"}
