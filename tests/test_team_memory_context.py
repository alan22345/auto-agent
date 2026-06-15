"""Tests for agent/context/memory.py routed through shared.memory_client."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.context.memory import query_relevant_memory


class TestQueryRelevantMemory:
    @pytest.mark.asyncio
    async def test_returns_formatted_memory_on_match(self):
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

        with patch("shared.memory_client.configured", return_value=True), \
             patch("shared.memory_client.recall", AsyncMock(return_value=mock_result)):
            result = await query_relevant_memory("update gemini provider")

        assert "Shared Team Memory" in result
        assert "gemini-provider" in result
        assert "gemini-2.5-pro" in result
        assert "[decision]" in result
        assert "(source: alan)" in result

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_matches(self):
        with patch("shared.memory_client.configured", return_value=True), \
             patch("shared.memory_client.recall", AsyncMock(return_value={"matches": [], "ambiguous": False})):
            result = await query_relevant_memory("update gemini provider")
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_when_ambiguous(self):
        mock_result = {
            "matches": [
                {"entity": {"id": "1", "name": "provider-alpha", "type": "provider", "tags": []}, "facts": [], "relevance": "fuzzy_match"},
                {"entity": {"id": "2", "name": "provider-beta", "type": "provider", "tags": []}, "facts": [], "relevance": "fuzzy_match"},
            ],
            "ambiguous": True,
            "hint": "Multiple entities matched with similar relevance.",
        }
        with patch("shared.memory_client.configured", return_value=True), \
             patch("shared.memory_client.recall", AsyncMock(return_value=mock_result)):
            result = await query_relevant_memory("provider")
        assert result == ""

    @pytest.mark.asyncio
    async def test_logs_warning_and_returns_empty_on_exception(self):
        with patch("shared.memory_client.configured", return_value=True), \
             patch("shared.memory_client.recall", AsyncMock(side_effect=RuntimeError("backend unreachable"))):
            result = await query_relevant_memory("some task")
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_for_empty_description(self):
        result = await query_relevant_memory("")
        assert result == ""
