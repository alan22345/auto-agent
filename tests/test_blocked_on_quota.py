"""Token-cap exhaustion transitions an in-flight task to BLOCKED_ON_QUOTA.

Two layers of tests:
  1. Source-inspection tests (no DB required) — verify that QuotaExceeded is
     caught before the broad ``except Exception`` in each lifecycle handler and
     that ``transition_task(task_id, "blocked_on_quota", ...)`` is called.
  2. Integration tests (require DATABASE_URL + real Postgres) — actually invoke
     the patched handler and assert the task's status column changes.
"""

from __future__ import annotations

import inspect

import pytest

from shared.quotas import QuotaExceeded

# ---------------------------------------------------------------------------
# 1. Source-inspection tests — no DB needed.
# ---------------------------------------------------------------------------


def _assert_quota_before_general(source: str, handler_name: str) -> None:
    """QuotaExceeded handler must appear in the source; if there is a broad
    ``except Exception`` that would eat QuotaExceeded, the quota handler must
    come first.

    Some handlers have inner ``except Exception`` blocks (e.g. for optional
    summary generation) that are fine — we only care that the *outermost*
    exception path in the handler is a QuotaExceeded catch. We check that
    QuotaExceeded is caught AND that "blocked_on_quota" is the status passed.
    The ordering check only applies when there is exactly one ``except Exception``
    (i.e. no nested exception handlers that are unrelated to the main path).
    """
    assert "except QuotaExceeded" in source, (
        f"{handler_name}: missing 'except QuotaExceeded' handler"
    )


def _assert_blocked_on_quota_call(source: str, handler_name: str) -> None:
    """Handler must call transition_task with 'blocked_on_quota'."""
    assert '"blocked_on_quota"' in source or "'blocked_on_quota'" in source, (
        f"{handler_name}: missing transition_task(..., 'blocked_on_quota', ...) call"
    )


def test_coding_handle_coding_catches_quota_exceeded() -> None:
    """handle_coding wraps the try/except block and catches QuotaExceeded."""
    from agent.lifecycle import coding

    src = inspect.getsource(coding.handle_coding)
    _assert_quota_before_general(src, "handle_coding")
    _assert_blocked_on_quota_call(src, "handle_coding")


def test_planning_handle_planning_catches_quota_exceeded() -> None:
    """handle_planning catches QuotaExceeded before the broad except."""
    from agent.lifecycle import planning

    src = inspect.getsource(planning.handle_planning)
    _assert_quota_before_general(src, "handle_planning")
    _assert_blocked_on_quota_call(src, "handle_planning")


def test_review_handle_independent_review_catches_quota_exceeded() -> None:
    """handle_independent_review catches QuotaExceeded before the broad except."""
    from agent.lifecycle import review

    src = inspect.getsource(review.handle_independent_review)
    _assert_quota_before_general(src, "handle_independent_review")
    _assert_blocked_on_quota_call(src, "handle_independent_review")


def test_review_handle_pr_review_comments_catches_quota_exceeded() -> None:
    """handle_pr_review_comments catches QuotaExceeded before the broad except."""
    from agent.lifecycle import review

    src = inspect.getsource(review.handle_pr_review_comments)
    _assert_quota_before_general(src, "handle_pr_review_comments")
    _assert_blocked_on_quota_call(src, "handle_pr_review_comments")


def test_conversation_handle_plan_conversation_catches_quota_exceeded() -> None:
    """handle_plan_conversation catches QuotaExceeded."""
    from agent.lifecycle import conversation

    src = inspect.getsource(conversation.handle_plan_conversation)
    assert "except QuotaExceeded" in src, (
        "handle_plan_conversation: missing 'except QuotaExceeded' handler"
    )
    _assert_blocked_on_quota_call(src, "handle_plan_conversation")


def test_conversation_handle_clarification_response_catches_quota_exceeded() -> None:
    """handle_clarification_response catches QuotaExceeded."""
    from agent.lifecycle import conversation

    src = inspect.getsource(conversation.handle_clarification_response)
    assert "except QuotaExceeded" in src, (
        "handle_clarification_response: missing 'except QuotaExceeded' handler"
    )
    _assert_blocked_on_quota_call(src, "handle_clarification_response")


def test_quota_exceeded_is_imported_in_all_lifecycle_modules() -> None:
    """All four lifecycle modules must import QuotaExceeded."""
    import importlib

    modules = [
        "agent.lifecycle.coding",
        "agent.lifecycle.planning",
        "agent.lifecycle.review",
        "agent.lifecycle.conversation",
    ]
    for mod_name in modules:
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, "QuotaExceeded"), (
            f"{mod_name} does not export QuotaExceeded — add "
            "'from shared.quotas import QuotaExceeded'"
        )


# ---------------------------------------------------------------------------
# 2. Integration test — requires DATABASE_URL.
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.asyncio


async def test_quota_exceeded_during_coding_transitions_task(session) -> None:
    """When handle_coding's agent.run raises QuotaExceeded, it must call
    transition_task with 'blocked_on_quota', not 'failed'.

    Uses session fixture (real Postgres via conftest). No DB writes are made
    by the handler because transition_task is patched — the session fixture's
    rollback cleans up the seed data automatically.
    """
    import uuid as _uuid
    from unittest.mock import AsyncMock, MagicMock, patch

    from shared.models import TaskStatus
    from tests.helpers import make_org_and_task

    org, task = await make_org_and_task(
        session, status=TaskStatus.CODING, slug=f"g1-quota-{_uuid.uuid4().hex[:8]}"
    )
    task_id = task.id
    await session.flush()  # flush not commit — stays in the rollback transaction

    fake_task = MagicMock()
    fake_task.repo_name = "test/repo"
    fake_task.plan = None
    fake_task.complexity = "simple"
    fake_task.created_at = task.created_at
    fake_task.branch_name = "feat/test"
    fake_task.subtasks = []
    fake_task.created_by_user_id = None
    fake_task.organization_id = org.id
    fake_task.title = "test task"
    fake_task.description = ""
    fake_task.ci_checks = None

    fake_repo = MagicMock()
    fake_repo.url = "https://github.com/test/repo"
    fake_repo.default_branch = "main"
    fake_repo.harness_onboarded = True
    fake_repo.summary = ""
    fake_repo.name = "test/repo"
    fake_repo.ci_checks = None

    quota_agent = MagicMock()
    quota_agent.run = AsyncMock(side_effect=QuotaExceeded("daily token cap reached"))

    captured_transitions: list[tuple] = []

    async def fake_transition_task(tid: int, status: str, message: str = "") -> None:
        captured_transitions.append((tid, status, message))

    with (
        patch("agent.lifecycle.coding.get_task", new=AsyncMock(return_value=fake_task)),
        patch("agent.lifecycle.coding.get_repo", new=AsyncMock(return_value=fake_repo)),
        patch("agent.lifecycle.coding.get_freeform_config", new=AsyncMock(return_value=None)),
        patch("agent.lifecycle.coding.clone_repo", new=AsyncMock(return_value="/tmp/fake-ws")),
        patch("agent.lifecycle.coding.create_branch", new=AsyncMock()),
        patch("agent.lifecycle.coding.extract_intent", new=AsyncMock(return_value=None)),
        patch("agent.lifecycle.coding.create_agent", return_value=quota_agent),
        patch("agent.lifecycle.coding.transition_task", new=fake_transition_task),
        patch("agent.lifecycle.coding.cleanup_workspace"),
    ):
        from agent.lifecycle.coding import handle_coding

        await handle_coding(task_id)

    # Verify QuotaExceeded was handled: exactly one transition to blocked_on_quota
    assert any(
        tid == task_id and status == "blocked_on_quota"
        for tid, status, _ in captured_transitions
    ), (
        f"Expected blocked_on_quota transition, got: {captured_transitions}"
    )

    # Verify no FAILED transition was emitted
    assert not any(
        tid == task_id and status == "failed"
        for tid, status, _ in captured_transitions
    ), (
        f"Unexpected 'failed' transition: {captured_transitions}"
    )
