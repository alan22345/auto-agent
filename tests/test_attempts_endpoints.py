"""Tests for GET /api/tasks/{id}/verify-attempts and /review-attempts."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.router import list_review_attempts, list_verify_attempts
from shared.models import ReviewAttempt, Task, TaskStatus, VerifyAttempt


def _mock_task(task_id: int = 1):
    t = MagicMock(spec=Task)
    t.id = task_id
    t.status = TaskStatus.VERIFYING
    return t


def _mock_query_result(rows):
    """Build the chained .execute().scalars().all() return shape."""
    result = MagicMock()
    result.scalars.return_value = MagicMock(all=MagicMock(return_value=rows))
    return result


@pytest.mark.asyncio
async def test_list_verify_attempts_returns_rows_oldest_first():
    rows = [
        VerifyAttempt(
            id=1, task_id=1, cycle=1, status="fail",
            boot_check="fail", intent_check=None,
            intent_judgment=None, tool_calls=None,
            failure_reason="boot_timeout", log_tail="boom",
            started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
        ),
        VerifyAttempt(
            id=2, task_id=1, cycle=2, status="pass",
            boot_check="pass", intent_check="pass",
            intent_judgment="looks good", tool_calls=[],
            failure_reason=None, log_tail=None,
            started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
        ),
    ]
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_mock_query_result(rows))

    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=_mock_task()),
    ):
        out = await list_verify_attempts(task_id=1, session=session, org_id=1)

    assert len(out) == 2
    assert out[0].cycle == 1
    assert out[0].boot_check == "fail"
    assert out[1].status == "pass"


@pytest.mark.asyncio
async def test_list_verify_attempts_404_when_task_missing():
    session = AsyncMock(spec=AsyncSession)
    with patch(
        "orchestrator.router._get_task_in_org", AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await list_verify_attempts(task_id=999, session=session, org_id=1)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_review_attempts_returns_rows():
    rows = [
        ReviewAttempt(
            id=1, task_id=1, cycle=1, status="pass",
            code_review_verdict="OK", ui_check="skipped",
            ui_judgment=None, tool_calls=[],
            failure_reason=None, log_tail=None,
            started_at=datetime.now(UTC), finished_at=datetime.now(UTC),
        ),
    ]
    session = AsyncMock(spec=AsyncSession)
    session.execute = AsyncMock(return_value=_mock_query_result(rows))

    with patch(
        "orchestrator.router._get_task_in_org",
        AsyncMock(return_value=_mock_task()),
    ):
        out = await list_review_attempts(task_id=1, session=session, org_id=1)

    assert len(out) == 1
    assert out[0].code_review_verdict == "OK"
    assert out[0].ui_check == "skipped"


@pytest.mark.asyncio
async def test_list_review_attempts_404_when_task_missing():
    session = AsyncMock(spec=AsyncSession)
    with patch(
        "orchestrator.router._get_task_in_org", AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await list_review_attempts(task_id=999, session=session, org_id=1)
    assert exc.value.status_code == 404
