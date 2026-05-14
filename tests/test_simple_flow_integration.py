"""Simple-flow integration — ADR-015 §5 Phase 4.

Verifies the end-to-end ordering for a simple-classified task with
``needs_grill=False``:

    grill (conditional, skipped) → coding (one-shot) → PR → PR-review
      (correctness scope) → DONE

We don't boot the LLM, don't run the dev server, and don't push to
GitHub — every step is mocked. What we DO assert is the orchestrator
walks through the right state transitions in the right order and the
PR-reviewer is invoked exactly once, with ``scope="correctness"``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.lifecycle.planning import _should_run_grill
from agent.prompts import GRILL_DONE_QUESTION_SENTINEL

# ---------------------------------------------------------------------------
# Phase-2 carryover: needs_grill=False ⇒ intake_qa=[] ⇒ planning skips grill.
# ---------------------------------------------------------------------------


def test_simple_task_with_needs_grill_false_skips_grill() -> None:
    """If the classifier wrote intake_qa=[] (needs_grill=False translation),
    the planner's grill gate must answer 'don't run grill'."""

    task = MagicMock()
    task.complexity = "simple"
    task.intake_qa = []  # ← classifier's needs_grill=False sentinel
    assert _should_run_grill(task) is False


def test_simple_task_post_grill_done_skips_grill() -> None:
    """Same gate, the post-grill case — also must skip."""

    task = MagicMock()
    task.complexity = "simple"
    task.intake_qa = [
        {"question": "X?", "answer": "Y"},
        {"question": GRILL_DONE_QUESTION_SENTINEL, "answer": "done"},
    ]
    assert _should_run_grill(task) is False


# ---------------------------------------------------------------------------
# End-to-end ordering: coding → PR → PR-review → DONE.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_flow_calls_pr_review_after_pr_creation() -> None:
    """The simple-classified path through coding._open_pr_and_advance must
    end at run_pr_review(scope='correctness'), not at the legacy
    handle_independent_review.

    The state-transition order observed by the orchestrator is:
      CODING → PR_CREATED → PR_REVIEW → DONE
    """

    transitions: list[tuple[str, str]] = []

    async def fake_transition_task(task_id: int, status: str, message: str = "") -> None:
        transitions.append((status, message))

    # A pretend simple task — only the attributes _open_pr_and_advance reads.
    task = MagicMock()
    task.id = 42
    task.title = "Simple task"
    task.description = "Make a thing"
    task.complexity = "simple"
    task.created_by_user_id = None
    task.organization_id = None
    task.parent_task_id = None
    task.freeform_mode = False
    task.repo_name = "demo"
    task.repo = MagicMock(default_branch="main")
    task.branch_name = "feat/simple"
    task.pr_url = "http://gh/pr/1"
    task.affected_routes = []

    from agent.lifecycle import coding

    fake_pr_review = AsyncMock(
        return_value=MagicMock(
            verdict="approved",
            comments=[],
            summary="all good",
        )
    )

    with (
        patch.object(coding, "commit_pending_changes", AsyncMock(return_value=False)),
        patch.object(coding, "ensure_branch_has_commits", AsyncMock()),
        patch.object(coding, "push_branch", AsyncMock()),
        patch.object(coding.review, "create_pr", AsyncMock(return_value="http://gh/pr/1")),
        patch.object(coding.review, "handle_independent_review", AsyncMock()) as legacy_review,
        patch("agent.lifecycle.pr_reviewer.run_pr_review", fake_pr_review),
        patch.object(coding, "transition_task", AsyncMock(side_effect=fake_transition_task)),
    ):
        await coding._open_pr_and_advance(
            task_id=42,
            task=task,
            workspace="/tmp/ws",
            base_branch="main",
            branch_name="feat/simple",
        )

    # PR-reviewer ran with the correctness scope (the only scope this phase
    # implements). Legacy review did NOT run for simple-classified tasks.
    assert fake_pr_review.await_count == 1
    kwargs = fake_pr_review.await_args.kwargs
    assert kwargs.get("scope") == "correctness"
    assert legacy_review.await_count == 0

    statuses = [s for s, _ in transitions]
    # The exact order: pr_created → pr_review → done. (Final status is DONE
    # because the fake review approved.)
    assert statuses == ["pr_created", "pr_review", "done"], statuses


@pytest.mark.asyncio
async def test_simple_flow_blocks_on_failed_pr_review() -> None:
    """A failing PR-review pushes the simple task to BLOCKED, not DONE."""

    transitions: list[tuple[str, str]] = []

    async def fake_transition_task(task_id: int, status: str, message: str = "") -> None:
        transitions.append((status, message))

    task = MagicMock()
    task.id = 7
    task.title = "Simple task"
    task.description = "X"
    task.complexity = "simple"
    task.created_by_user_id = None
    task.organization_id = None
    task.parent_task_id = None
    task.freeform_mode = False
    task.repo_name = "demo"
    task.repo = MagicMock(default_branch="main")
    task.branch_name = "feat/simple"
    task.pr_url = "http://gh/pr/2"
    task.affected_routes = []

    from agent.lifecycle import coding

    fake_pr_review = AsyncMock(
        return_value=MagicMock(
            verdict="changes_requested",
            comments=[{"path": "x.py", "line": 1, "comment": "stub"}],
            summary="found stub",
        )
    )

    with (
        patch.object(coding, "commit_pending_changes", AsyncMock(return_value=False)),
        patch.object(coding, "ensure_branch_has_commits", AsyncMock()),
        patch.object(coding, "push_branch", AsyncMock()),
        patch.object(coding.review, "create_pr", AsyncMock(return_value="http://gh/pr/2")),
        patch.object(coding.review, "handle_independent_review", AsyncMock()),
        patch("agent.lifecycle.pr_reviewer.run_pr_review", fake_pr_review),
        patch.object(coding, "transition_task", AsyncMock(side_effect=fake_transition_task)),
    ):
        await coding._open_pr_and_advance(
            task_id=7,
            task=task,
            workspace="/tmp/ws",
            base_branch="main",
            branch_name="feat/simple",
        )

    statuses = [s for s, _ in transitions]
    # On fail: pr_created → pr_review → blocked.
    assert statuses == ["pr_created", "pr_review", "blocked"], statuses
    # The block reason should expose the review failure for humans.
    blocked_msg = transitions[-1][1]
    assert "stub" in blocked_msg.lower() or "changes_requested" in blocked_msg
