"""Tests for memory context injection."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.context.memory import query_relevant_memory


class TestQueryRelevantMemory:
    @pytest.mark.asyncio
    async def test_returns_empty_for_no_matches(self):
        with patch("agent.context.memory._search_nodes", new_callable=AsyncMock, return_value=[]):
            result = await query_relevant_memory("build a todo app")
            assert result == ""

    @pytest.mark.asyncio
    async def test_returns_empty_for_empty_description(self):
        result = await query_relevant_memory("")
        assert result == ""

    @pytest.mark.asyncio
    async def test_returns_formatted_context_for_matches(self):
        node = MagicMock()
        node.id = uuid.uuid4()
        node.name = "frontend-stack"
        node.node_type = "preference"
        node.content = "use nextjs, tailwind"
        node.outgoing_edges = []
        node.incoming_edges = []

        with patch("agent.context.memory._search_nodes", new_callable=AsyncMock, return_value=[node]):
            result = await query_relevant_memory("build a frontend")
            assert "frontend-stack" in result
            assert "nextjs" in result
            assert "Shared Team Memory" in result

    @pytest.mark.asyncio
    async def test_deduplicates_nodes(self):
        node_id = uuid.uuid4()
        node = MagicMock()
        node.id = node_id
        node.name = "python-tooling"
        node.node_type = "preference"
        node.content = "use uv"
        node.outgoing_edges = []
        node.incoming_edges = []

        with patch("agent.context.memory._search_nodes", new_callable=AsyncMock, return_value=[node]):
            result = await query_relevant_memory("python project tooling setup")
            # Even though multiple keywords match, node appears only once
            assert result.count("python-tooling") == 1

    @pytest.mark.asyncio
    async def test_skips_stop_words(self):
        with patch("agent.context.memory._search_nodes", new_callable=AsyncMock, return_value=[]) as mock:
            await query_relevant_memory("please create a new thing")
            # "please", "create", "a", "new" are all stop words; only "thing" should be searched
            assert mock.call_count <= 1
