"""Tests for GET /api/tasks/{id}/architect-attempts and /trio-review-attempts."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.router import list_architect_attempts, list_trio_review_attempts
from shared.models import ArchitectAttempt, Task, TaskStatus, TrioReviewAttempt


def _mock_task(task_id: int = 1):
    t = MagicMock(spec=Task)
    t.id = task_id
    t.status = TaskStatus.PLANNING
    return t


def _mock_query_result(rows):
    """Build the chained .execute().scalars().all() return shape."""
    result = MagicMock()
    result.scalars.return_value = MagicMock(all=MagicMock(return_value=rows))
    return result


@pytest.mark.asyncio
async def test_get_architect_attempts_returns_rows_in_order():
    """Seed 2 architect_attempts rows for a task. Assert 200 + JSON list with both rows ordered by created_at ascending."""
    now = datetime.now(UTC)
    rows = [
        ArchitectAttempt(
            id=1, task_id=1, phase="initial", cycle=1,
            reasoning="First pass",
            decision=None, consult_question=None, consult_why=None,
            architecture_md_after=None, commit_sha=None,
            tool_calls=[],
            created_at=now,
        ),
        ArchitectAttempt(
            id=2, task_id=1, phase="checkpoint", cycle=2,
            reasoning="Second pass",
            decision={"action": "done"},
            consult_question=None, consult_why=None,
            architecture_md_after="# arch", commit_sha="abc1234",
            tool_calls=[{"name": "bash"}],
            created_at=now,
        ),
    ]
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_mock_query_result(rows))

    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=_mock_task()),
    ):
        out = await list_architect_attempts(task_id=1, session=session, org_id=1)

    assert len(out) == 2
    assert out[0].cycle == 1
    assert out[0].phase == "initial"
    assert out[0].reasoning == "First pass"
    assert out[1].cycle == 2
    assert out[1].phase == "checkpoint"
    assert out[1].decision == {"action": "done"}
    assert out[1].commit_sha == "abc1234"


@pytest.mark.asyncio
async def test_get_architect_attempts_empty_when_no_rows():
    """No rows for a task. Hit the endpoint. Assert 200 + []."""
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_mock_query_result([]))

    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=_mock_task()),
    ):
        out = await list_architect_attempts(task_id=1, session=session, org_id=1)

    assert out == []


@pytest.mark.asyncio
async def test_get_architect_attempts_404_when_task_missing():
    session = AsyncMock(spec=AsyncSession)
    with patch(
        "orchestrator.router._get_task_in_org", AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await list_architect_attempts(task_id=999, session=session, org_id=1)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_get_trio_review_attempts_returns_rows():
    """Seed 2 trio_review_attempts rows. Assert 200 + JSON list with both rows."""
    now = datetime.now(UTC)
    rows = [
        TrioReviewAttempt(
            id=1, task_id=1, cycle=1,
            ok=False, feedback="Needs more work",
            tool_calls=[],
            created_at=now,
        ),
        TrioReviewAttempt(
            id=2, task_id=1, cycle=2,
            ok=True, feedback="LGTM",
            tool_calls=[{"name": "grep"}],
            created_at=now,
        ),
    ]
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_mock_query_result(rows))

    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=_mock_task()),
    ):
        out = await list_trio_review_attempts(task_id=1, session=session, org_id=1)

    assert len(out) == 2
    assert out[0].cycle == 1
    assert out[0].ok is False
    assert out[0].feedback == "Needs more work"
    assert out[1].cycle == 2
    assert out[1].ok is True
    assert out[1].feedback == "LGTM"


@pytest.mark.asyncio
async def test_get_trio_review_attempts_404_when_task_missing():
    session = AsyncMock(spec=AsyncSession)
    with patch(
        "orchestrator.router._get_task_in_org", AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await list_trio_review_attempts(task_id=999, session=session, org_id=1)
    assert exc.value.status_code == 404
