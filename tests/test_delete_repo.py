"""Tests for DELETE /api/repos/{repo_name} endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import (
    Repo,
    Task,
    TaskStatus,
)
from orchestrator.router import delete_repo, TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(id: int = 1, name: str = "test-repo") -> MagicMock:
    repo = MagicMock(spec=Repo)
    repo.id = id
    repo.name = name
    repo.url = f"https://github.com/org/{name}"
    return repo


def _make_task(id: int = 1, repo_id: int = 1, status: TaskStatus = TaskStatus.DONE) -> MagicMock:
    task = MagicMock(spec=Task)
    task.id = id
    task.repo_id = repo_id
    task.status = status
    return task


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTerminalStatuses:
    def test_done_is_terminal(self):
        assert TaskStatus.DONE in TERMINAL_STATUSES

    def test_failed_is_terminal(self):
        assert TaskStatus.FAILED in TERMINAL_STATUSES

    def test_blocked_is_terminal(self):
        assert TaskStatus.BLOCKED in TERMINAL_STATUSES

    def test_coding_is_not_terminal(self):
        assert TaskStatus.CODING not in TERMINAL_STATUSES

    def test_intake_is_not_terminal(self):
        assert TaskStatus.INTAKE not in TERMINAL_STATUSES


class TestDeleteRepoEndpoint:
    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_by_name", new_callable=AsyncMock)
    async def test_not_found(self, mock_get_repo):
        mock_get_repo.return_value = None
        session = AsyncMock(spec=AsyncSession)

        with pytest.raises(HTTPException) as exc_info:
            await delete_repo("nonexistent", session)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_by_name", new_callable=AsyncMock)
    async def test_active_task_blocks_deletion(self, mock_get_repo, publisher):
        repo = _make_repo()
        mock_get_repo.return_value = repo
        active_task = _make_task(status=TaskStatus.CODING)

        session = AsyncMock(spec=AsyncSession)
        # First execute: active task query
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [active_task]
        session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await delete_repo("test-repo", session)
        assert exc_info.value.status_code == 409
        assert "1 active task(s)" in exc_info.value.detail

        # Verify no deletes happened and no event was published
        session.delete.assert_not_called()
        session.commit.assert_not_called()
        assert publisher.events == []

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_by_name", new_callable=AsyncMock)
    async def test_successful_delete(self, mock_get_repo, publisher):
        repo = _make_repo()
        mock_get_repo.return_value = repo

        session = AsyncMock(spec=AsyncSession)
        # First execute: active task query returns empty
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session.execute.side_effect = [
            mock_result,  # active task check
            None,         # delete suggestions
            None,         # delete freeform config
            None,         # update tasks (orphan)
        ]

        result = await delete_repo("test-repo", session)

        assert result == {"deleted": "test-repo"}
        session.delete.assert_called_once_with(repo)
        session.commit.assert_called_once()
        # 4 execute calls: active check, delete suggestions, delete config, orphan tasks
        assert session.execute.call_count == 4

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_by_name", new_callable=AsyncMock)
    async def test_blocked_task_allows_deletion(self, mock_get_repo, publisher):
        """BLOCKED tasks should not prevent repo deletion."""
        repo = _make_repo()
        mock_get_repo.return_value = repo

        session = AsyncMock(spec=AsyncSession)
        # Active task query returns empty (BLOCKED is terminal)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session.execute.side_effect = [mock_result, None, None, None]

        result = await delete_repo("test-repo", session)
        assert result == {"deleted": "test-repo"}

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_by_name", new_callable=AsyncMock)
    async def test_publishes_event(self, mock_get_repo, publisher):
        repo = _make_repo()
        mock_get_repo.return_value = repo

        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session.execute.side_effect = [mock_result, None, None, None]

        await delete_repo("test-repo", session)

        assert len(publisher.events) == 1
        ev = publisher.events[0]
        assert ev.type == "repo.deleted"
        assert ev.payload == {"repo_name": "test-repo"}

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_by_name", new_callable=AsyncMock)
    async def test_multiple_active_tasks_reports_count(self, mock_get_repo, publisher):
        repo = _make_repo()
        mock_get_repo.return_value = repo

        tasks = [_make_task(id=i, status=TaskStatus.CODING) for i in range(3)]
        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = tasks
        session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc_info:
            await delete_repo("test-repo", session)
        assert "3 active task(s)" in exc_info.value.detail
