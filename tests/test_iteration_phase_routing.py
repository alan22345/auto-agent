"""ADR-017 — route_human_message dispatches AWAITING_REVIEW / ITERATING
complex_large tasks to iteration.handle_iteration_feedback. Non-trio
PR_CREATED tasks still go to the legacy handle_pr_review_comments."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.events import Event, HumanEventType


@pytest.mark.asyncio
async def test_routes_complex_large_awaiting_review_to_iteration():
    from agent.lifecycle import conversation

    fake_task = SimpleNamespace(
        id=5, status="awaiting_review", complexity="complex_large",
        pr_url="https://x/pr/1",
    )
    iteration_mock = AsyncMock()
    with (
        patch.object(conversation, "get_task", AsyncMock(return_value=fake_task)),
        patch(
            "agent.lifecycle.trio.iteration.handle_iteration_feedback",
            iteration_mock,
        ),
    ):
        await conversation.route_human_message(Event(
            type=HumanEventType.MESSAGE,
            task_id=5,
            payload={"message": "break it down further"},
        ))

    iteration_mock.assert_awaited_once_with(5, "break it down further")


@pytest.mark.asyncio
async def test_routes_complex_large_iterating_to_iteration():
    from agent.lifecycle import conversation

    fake_task = SimpleNamespace(
        id=5, status="iterating", complexity="complex_large",
        pr_url="https://x/pr/1",
    )
    iteration_mock = AsyncMock()
    with (
        patch.object(conversation, "get_task", AsyncMock(return_value=fake_task)),
        patch(
            "agent.lifecycle.trio.iteration.handle_iteration_feedback",
            iteration_mock,
        ),
    ):
        await conversation.route_human_message(Event(
            type=HumanEventType.MESSAGE,
            task_id=5,
            payload={"message": "also do Y"},
        ))

    iteration_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_routes_non_trio_pr_created_to_legacy_handler():
    """Non-trio (simple/complex) PR_CREATED tasks keep the existing
    handle_pr_review_comments path — that flow has its own iteration."""
    from agent.lifecycle import conversation

    fake_task = SimpleNamespace(
        id=7, status="pr_created", complexity="complex",
        pr_url="https://x/pr/2",
    )
    legacy_mock = AsyncMock()
    iter_mock = AsyncMock()
    with (
        patch.object(conversation, "get_task", AsyncMock(return_value=fake_task)),
        patch.object(conversation.review, "handle_pr_review_comments", legacy_mock),
        patch(
            "agent.lifecycle.trio.iteration.handle_iteration_feedback",
            iter_mock,
        ),
    ):
        await conversation.route_human_message(Event(
            type=HumanEventType.MESSAGE,
            task_id=7,
            payload={"message": "fix the indentation"},
        ))

    legacy_mock.assert_awaited_once()
    iter_mock.assert_not_awaited()
