"""Tests for repo-map storage via team-memory in agent/context/system.py."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from agent.context.system import SystemPromptBuilder
from team_memory.graph import GraphEngine
from team_memory.models import Entity, Fact


def _make_session_cm(session):
    """Return an async context manager that yields the given session."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestRepoMapLoad:
    @pytest.mark.asyncio
    async def test_load_returns_none_when_no_entity(self):
        """_load_repo_map_from_memory returns None when entity doesn't exist."""
        mock_session = MagicMock()
        mock_engine = AsyncMock(spec=GraphEngine)
        mock_engine.resolve = AsyncMock(return_value=[])

        with patch("agent.context.system.team_memory_session", return_value=_make_session_cm(mock_session)):
            with patch("agent.context.system.GraphEngine", return_value=mock_engine):
                ctx = SystemPromptBuilder.__new__(SystemPromptBuilder)
                result = await ctx._load_repo_map_from_memory("my-repo")

        assert result is None

    @pytest.mark.asyncio
    async def test_load_returns_latest_fact_content(self):
        """_load_repo_map_from_memory returns content of the most recent current fact."""
        entity_id = uuid.uuid4()
        ent = MagicMock(spec=Entity)
        ent.id = entity_id
        ent.name = "repo-map:my-repo"

        match = MagicMock()
        match.entity = ent

        fact = MagicMock(spec=Fact)
        fact.content = "commit:abc\n---\n  app.py"

        mock_session = MagicMock()
        mock_engine = AsyncMock(spec=GraphEngine)
        mock_engine.resolve = AsyncMock(return_value=[match])
        mock_engine._facts_for = AsyncMock(return_value=[fact])

        with patch("agent.context.system.team_memory_session", return_value=_make_session_cm(mock_session)):
            with patch("agent.context.system.GraphEngine", return_value=mock_engine):
                ctx = SystemPromptBuilder.__new__(SystemPromptBuilder)
                result = await ctx._load_repo_map_from_memory("my-repo")

        assert result == "commit:abc\n---\n  app.py"
        mock_engine._facts_for.assert_awaited_once_with(entity_id)

    @pytest.mark.asyncio
    async def test_load_returns_none_on_exception(self):
        """_load_repo_map_from_memory returns None and swallows exceptions."""
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(side_effect=RuntimeError("DB gone"))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agent.context.system.team_memory_session", return_value=mock_session):
            ctx = SystemPromptBuilder.__new__(SystemPromptBuilder)
            result = await ctx._load_repo_map_from_memory("my-repo")

        assert result is None


class TestRepoMapStore:
    @pytest.mark.asyncio
    async def test_store_calls_remember_when_no_existing_entity(self):
        """_store_repo_map_to_memory calls remember() when no entity exists."""
        mock_session = MagicMock()
        mock_session.commit = AsyncMock()

        mock_engine = AsyncMock(spec=GraphEngine)
        mock_engine.resolve = AsyncMock(return_value=[])
        mock_engine.remember = AsyncMock(return_value={"entity_id": "x", "fact_id": "y"})

        with patch("agent.context.system.team_memory_session", return_value=_make_session_cm(mock_session)):
            with patch("agent.context.system.GraphEngine", return_value=mock_engine):
                ctx = SystemPromptBuilder.__new__(SystemPromptBuilder)
                await ctx._store_repo_map_to_memory("new-repo", "some content")

        mock_engine.remember.assert_awaited_once_with(
            content="some content",
            entity="repo-map:new-repo",
            entity_type="repo-map",
            kind="config",
        )
        mock_engine.correct.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_store_calls_correct_when_existing_fact(self):
        """_store_repo_map_to_memory calls correct() when an existing fact is found."""
        fact_id = str(uuid.uuid4())
        entity_id = uuid.uuid4()

        ent = MagicMock(spec=Entity)
        ent.id = entity_id

        match = MagicMock()
        match.entity = ent

        existing_fact = MagicMock(spec=Fact)
        existing_fact.id = uuid.UUID(fact_id)

        mock_session = MagicMock()
        mock_session.commit = AsyncMock()

        mock_engine = AsyncMock(spec=GraphEngine)
        mock_engine.resolve = AsyncMock(return_value=[match])
        mock_engine._facts_for = AsyncMock(return_value=[existing_fact])
        mock_engine.correct = AsyncMock(return_value={"old_fact_id": fact_id, "new_fact_id": "nf"})

        with patch("agent.context.system.team_memory_session", return_value=_make_session_cm(mock_session)):
            with patch("agent.context.system.GraphEngine", return_value=mock_engine):
                ctx = SystemPromptBuilder.__new__(SystemPromptBuilder)
                await ctx._store_repo_map_to_memory("existing-repo", "updated content")

        mock_engine.correct.assert_awaited_once_with(
            fact_id=fact_id,
            new_content="updated content",
            reason="repo updated",
        )
        mock_engine.remember.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_store_swallows_exception(self):
        """_store_repo_map_to_memory logs warning and swallows exceptions."""
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(side_effect=RuntimeError("DB gone"))
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("agent.context.system.team_memory_session", return_value=mock_cm):
            ctx = SystemPromptBuilder.__new__(SystemPromptBuilder)
            # Should not raise
            await ctx._store_repo_map_to_memory("bad-repo", "content")
