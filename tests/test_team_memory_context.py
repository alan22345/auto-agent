"""Tests for agent/context/memory.py using team-memory GraphEngine."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from agent.context.memory import query_relevant_memory


class TestQueryRelevantMemory:
    @pytest.mark.asyncio
    async def test_returns_formatted_memory_on_match(self):
        """query_relevant_memory returns formatted bullet list when a matching entity exists."""
        mock_result = {
            "matches": [
                {
                    "entity": {
                        "id": "abc-123",
                        "name": "gemini-provider",
                        "type": "provider",
                        "tags": [],
                    },
                    "facts": [
                        {
                            "id": "f1",
                            "content": "Use gemini-2.5-pro for large context tasks",
                            "kind": "decision",
                            "valid_from": None,
                            "valid_until": None,
                            "source": "alan",
                        }
                    ],
                    "relevance": "direct_name_match",
                }
            ],
            "ambiguous": False,
        }

        mock_engine = AsyncMock()
        mock_engine.recall = AsyncMock(return_value=mock_result)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agent.context.memory.team_memory_session", return_value=mock_session):
            with patch("agent.context.memory.GraphEngine", return_value=mock_engine):
                result = await query_relevant_memory("update gemini provider")

        assert "Shared Team Memory" in result
        assert "gemini-provider" in result
        assert "gemini-2.5-pro" in result
        assert "[decision]" in result
        assert "(source: alan)" in result

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_matches(self):
        """query_relevant_memory returns empty string when no entities match."""
        mock_result = {"matches": [], "ambiguous": False}

        mock_engine = AsyncMock()
        mock_engine.recall = AsyncMock(return_value=mock_result)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agent.context.memory.team_memory_session", return_value=mock_session):
            with patch("agent.context.memory.GraphEngine", return_value=mock_engine):
                result = await query_relevant_memory("update gemini provider")

        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_when_ambiguous(self):
        """query_relevant_memory returns empty string when result is ambiguous."""
        mock_result = {
            "matches": [
                {
                    "entity": {"id": "1", "name": "provider-alpha", "type": "provider", "tags": []},
                    "facts": [],
                    "relevance": "fuzzy_match",
                },
                {
                    "entity": {"id": "2", "name": "provider-beta", "type": "provider", "tags": []},
                    "facts": [],
                    "relevance": "fuzzy_match",
                },
            ],
            "ambiguous": True,
            "hint": "Multiple entities matched with similar relevance.",
        }

        mock_engine = AsyncMock()
        mock_engine.recall = AsyncMock(return_value=mock_result)

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agent.context.memory.team_memory_session", return_value=mock_session):
            with patch("agent.context.memory.GraphEngine", return_value=mock_engine):
                result = await query_relevant_memory("provider")

        assert result == ""

    @pytest.mark.asyncio
    async def test_logs_warning_and_returns_empty_on_exception(self):
        """query_relevant_memory returns empty string and logs warning on DB error."""
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(side_effect=RuntimeError("DB unreachable"))
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("agent.context.memory.team_memory_session", return_value=mock_session):
            result = await query_relevant_memory("some task")

        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_for_empty_description(self):
        """query_relevant_memory returns empty string for empty input."""
        result = await query_relevant_memory("")
        assert result == ""
