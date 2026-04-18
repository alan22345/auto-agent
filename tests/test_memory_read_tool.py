"""Tests for the memory_read agent tool."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.memory_read import MemoryReadTool


@pytest.fixture
def tool():
    return MemoryReadTool()


@pytest.fixture
def ctx():
    return ToolContext(workspace="/tmp/test")


class TestMemoryReadToolDefinition:
    def test_name(self, tool):
        assert tool.name == "memory_read"

    def test_is_readonly(self, tool):
        assert tool.is_readonly is True

    def test_has_action_parameter(self, tool):
        assert "action" in tool.parameters["properties"]

    def test_actions_are_search_traverse_chain_roots(self, tool):
        actions = tool.parameters["properties"]["action"]["enum"]
        assert set(actions) == {"search", "traverse", "get_decision_chain", "list_roots"}


class TestMemoryReadSearch:
    @pytest.mark.asyncio
    async def test_search_returns_matching_nodes(self, tool, ctx):
        fake_node_id = str(uuid.uuid4())
        mock_node = MagicMock()
        mock_node.id = uuid.UUID(fake_node_id)
        mock_node.name = "python-tooling"
        mock_node.node_type = "preference"
        mock_node.content = "use uv, ruff, pytest"
        mock_node.outgoing_edges = []
        mock_node.incoming_edges = []

        with patch("agent.tools.memory_read._search_nodes", new_callable=AsyncMock, return_value=[mock_node]):
            result = await tool.execute({"action": "search", "query": "python"}, ctx)
            assert "python-tooling" in result.output
            assert not result.is_error

    @pytest.mark.asyncio
    async def test_search_requires_query(self, tool, ctx):
        result = await tool.execute({"action": "search"}, ctx)
        assert result.is_error
        assert "query" in result.output.lower()

    @pytest.mark.asyncio
    async def test_search_no_results(self, tool, ctx):
        with patch("agent.tools.memory_read._search_nodes", new_callable=AsyncMock, return_value=[]):
            result = await tool.execute({"action": "search", "query": "nonexistent"}, ctx)
            assert not result.is_error
            assert "No memory nodes found" in result.output


class TestMemoryReadListRoots:
    @pytest.mark.asyncio
    async def test_list_roots_returns_nodes_without_incoming(self, tool, ctx):
        fake_id = str(uuid.uuid4())
        mock_node = MagicMock()
        mock_node.id = uuid.UUID(fake_id)
        mock_node.name = "company-standards"
        mock_node.node_type = "root"
        mock_node.content = "Top-level standards"

        with patch("agent.tools.memory_read._list_root_nodes", new_callable=AsyncMock, return_value=[mock_node]):
            result = await tool.execute({"action": "list_roots"}, ctx)
            assert "company-standards" in result.output
            assert not result.is_error

    @pytest.mark.asyncio
    async def test_list_roots_empty_graph(self, tool, ctx):
        with patch("agent.tools.memory_read._list_root_nodes", new_callable=AsyncMock, return_value=[]):
            result = await tool.execute({"action": "list_roots"}, ctx)
            assert "empty" in result.output.lower()


class TestMemoryReadTraverse:
    @pytest.mark.asyncio
    async def test_traverse_requires_node_id(self, tool, ctx):
        result = await tool.execute({"action": "traverse"}, ctx)
        assert result.is_error
        assert "node_id" in result.output.lower()


class TestMemoryReadDecisionChain:
    @pytest.mark.asyncio
    async def test_chain_requires_node_id(self, tool, ctx):
        result = await tool.execute({"action": "get_decision_chain"}, ctx)
        assert result.is_error
        assert "node_id" in result.output.lower()

    @pytest.mark.asyncio
    async def test_chain_returns_formatted_decisions(self, tool, ctx):
        chain = [
            {"id": str(uuid.uuid4()), "name": "orm-choice", "type": "decision", "content": "switched to polars", "created_at": "2024-09-01T00:00:00"},
            {"id": str(uuid.uuid4()), "name": "orm-choice", "type": "decision", "content": "use pandas", "created_at": "2024-03-01T00:00:00"},
        ]
        with patch("agent.tools.memory_read._get_decision_chain", new_callable=AsyncMock, return_value=chain):
            result = await tool.execute({"action": "get_decision_chain", "node_id": str(uuid.uuid4())}, ctx)
            assert "CURRENT" in result.output
            assert "switched to polars" in result.output
            assert not result.is_error
