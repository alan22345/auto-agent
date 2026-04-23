"""Tests for POST/GET /api/tasks/{id}/messages."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.router import list_task_messages, post_task_message
from shared.models import Task, TaskStatus
from shared.types import TaskMessagePost


def _mock_task(task_id: int = 1):
    task = MagicMock(spec=Task)
    task.id = task_id
    task.status = TaskStatus.CODING
    return task


class TestPostTaskMessage:
    @pytest.mark.asyncio
    async def test_empty_content_rejected(self):
        session = AsyncMock(spec=AsyncSession)
        with patch("orchestrator.router.get_task", AsyncMock(return_value=_mock_task())):
            with pytest.raises(HTTPException) as exc:
                await post_task_message(
                    task_id=1,
                    req=TaskMessagePost(content="   "),
                    session=session,
                    authorization=None,
                    x_sender=None,
                )
            assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_task_rejected(self):
        session = AsyncMock(spec=AsyncSession)
        with patch("orchestrator.router.get_task", AsyncMock(return_value=None)):
            with pytest.raises(HTTPException) as exc:
                await post_task_message(
                    task_id=999,
                    req=TaskMessagePost(content="hello"),
                    session=session,
                    authorization=None,
                    x_sender=None,
                )
            assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_x_sender_header_used_when_no_auth(self):
        """Internal callers (Telegram bridge) pass X-Sender."""
        session = AsyncMock(spec=AsyncSession)
        # Simulate the row hydration from refresh()
        async def refresh_stub(msg):
            msg.id = 42
            msg.created_at = None
        session.refresh = refresh_stub
        fake_redis = AsyncMock()

        with patch("orchestrator.router.get_task", AsyncMock(return_value=_mock_task())), \
             patch("orchestrator.router.get_redis", AsyncMock(return_value=fake_redis)), \
             patch("orchestrator.router.publish_event", AsyncMock()):
            result = await post_task_message(
                task_id=1,
                req=TaskMessagePost(content="stop doing that"),
                session=session,
                authorization=None,
                x_sender="telegram:12345",
            )

        assert result.sender == "telegram:12345"
        assert result.content == "stop doing that"
        # Guidance pushed onto the agent's Redis queue for next-turn pickup
        fake_redis.rpush.assert_awaited_once()
        args, _ = fake_redis.rpush.await_args
        assert args[0] == "task:1:guidance"
        assert "telegram:12345: stop doing that" in args[1]


class TestListTaskMessages:
    @pytest.mark.asyncio
    async def test_unknown_task_404(self):
        session = AsyncMock(spec=AsyncSession)
        with patch("orchestrator.router.get_task", AsyncMock(return_value=None)):
            with pytest.raises(HTTPException) as exc:
                await list_task_messages(task_id=999, session=session)
            assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_messages_oldest_first(self):
        session = AsyncMock(spec=AsyncSession)
        row1 = MagicMock(id=1, task_id=1, sender="alan", content="first", created_at=None)
        row2 = MagicMock(id=2, task_id=1, sender="alan", content="second", created_at=None)
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = [row1, row2]
        exec_result = MagicMock()
        exec_result.scalars.return_value = scalars_mock
        session.execute = AsyncMock(return_value=exec_result)

        with patch("orchestrator.router.get_task", AsyncMock(return_value=_mock_task())):
            result = await list_task_messages(task_id=1, session=session)

        assert [m.content for m in result] == ["first", "second"]
