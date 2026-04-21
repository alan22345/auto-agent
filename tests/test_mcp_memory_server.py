"""Tests for MCP memory server tool functions."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestMCPMemorySearch:
    @pytest.mark.asyncio
    async def test_search_returns_formatted_nodes(self):
        """memory_search should return formatted node text."""
        from agent.mcp_memory_server import memory_search

        mock_node = MagicMock()
        mock_node.id = uuid.uuid4()
        mock_node.name = "auth-pattern"
        mock_node.node_type = "decision"
        mock_node.content = "Use JWT for auth"
        mock_node.outgoing_edges = []
        mock_node.incoming_edges = []

        with patch("agent.mcp_memory_server._search_nodes", new_callable=AsyncMock, return_value=[mock_node]):
            result = await memory_search("auth")

        assert "auth-pattern" in result
        assert "Use JWT for auth" in result

    @pytest.mark.asyncio
    async def test_search_returns_message_when_empty(self):
        from agent.mcp_memory_server import memory_search

        with patch("agent.mcp_memory_server._search_nodes", new_callable=AsyncMock, return_value=[]):
            result = await memory_search("nonexistent")

        assert "no" in result.lower() or "No" in result


class TestMCPMemoryListRoots:
    @pytest.mark.asyncio
    async def test_list_roots_returns_node_names(self):
        from agent.mcp_memory_server import memory_list_roots

        mock_node = MagicMock()
        mock_node.id = uuid.uuid4()
        mock_node.name = "project-config"
        mock_node.node_type = "project"

        with patch("agent.mcp_memory_server._list_root_nodes", new_callable=AsyncMock, return_value=[mock_node]):
            result = await memory_list_roots()

        assert "project-config" in result

    @pytest.mark.asyncio
    async def test_list_roots_empty_graph(self):
        from agent.mcp_memory_server import memory_list_roots

        with patch("agent.mcp_memory_server._list_root_nodes", new_callable=AsyncMock, return_value=[]):
            result = await memory_list_roots()

        assert "empty" in result.lower()


class TestMCPMemoryCreateNode:
    @pytest.mark.asyncio
    async def test_create_node_returns_confirmation(self):
        from agent.mcp_memory_server import memory_create_node

        fake_id = uuid.uuid4()

        async def mock_create(name, node_type, content, task_id):
            node = MagicMock()
            node.id = fake_id
            node.name = name
            return node

        with patch("agent.mcp_memory_server._create_node_db", new_callable=AsyncMock, side_effect=mock_create):
            result = await memory_create_node(
                name="test-decision",
                node_type="decision",
                content="We chose X over Y",
            )

        assert "test-decision" in result
        assert str(fake_id) in result
