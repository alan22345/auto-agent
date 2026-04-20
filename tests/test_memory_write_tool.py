"""Tests for the memory_write agent tool."""

import pytest

from agent.tools.base import ToolContext
from agent.tools.memory_write import MemoryWriteTool


@pytest.fixture
def tool():
    return MemoryWriteTool()


@pytest.fixture
def ctx():
    return ToolContext(workspace="/tmp/test")


class TestMemoryWriteToolDefinition:
    def test_name(self, tool):
        assert tool.name == "memory_write"

    def test_is_not_readonly(self, tool):
        assert tool.is_readonly is False

    def test_has_action_parameter(self, tool):
        assert "action" in tool.parameters["properties"]

    def test_actions(self, tool):
        actions = tool.parameters["properties"]["action"]["enum"]
        assert set(actions) == {
            "create_node", "create_edge", "append_decision", "update_node", "delete_node"
        }


class TestMemoryWriteValidation:
    @pytest.mark.asyncio
    async def test_create_node_requires_name(self, tool, ctx):
        result = await tool.execute(
            {"action": "create_node", "node_type": "test", "content": "x"}, ctx
        )
        assert result.is_error
        assert "name" in result.output.lower()

    @pytest.mark.asyncio
    async def test_create_node_requires_node_type(self, tool, ctx):
        result = await tool.execute(
            {"action": "create_node", "name": "test", "content": "x"}, ctx
        )
        assert result.is_error
        assert "node_type" in result.output.lower()

    @pytest.mark.asyncio
    async def test_create_edge_requires_all_fields(self, tool, ctx):
        result = await tool.execute(
            {"action": "create_edge", "source_id": "abc"}, ctx
        )
        assert result.is_error

    @pytest.mark.asyncio
    async def test_update_node_requires_node_id(self, tool, ctx):
        result = await tool.execute(
            {"action": "update_node", "content": "new"}, ctx
        )
        assert result.is_error
        assert "node_id" in result.output.lower()

    @pytest.mark.asyncio
    async def test_update_node_requires_content(self, tool, ctx):
        result = await tool.execute(
            {"action": "update_node", "node_id": "abc"}, ctx
        )
        assert result.is_error
        assert "content" in result.output.lower()

    @pytest.mark.asyncio
    async def test_delete_node_requires_node_id(self, tool, ctx):
        result = await tool.execute(
            {"action": "delete_node"}, ctx
        )
        assert result.is_error
        assert "node_id" in result.output.lower()

    @pytest.mark.asyncio
    async def test_append_decision_requires_node_id(self, tool, ctx):
        result = await tool.execute(
            {"action": "append_decision", "content": "new decision"}, ctx
        )
        assert result.is_error
        assert "node_id" in result.output.lower()

    @pytest.mark.asyncio
    async def test_append_decision_requires_content(self, tool, ctx):
        result = await tool.execute(
            {"action": "append_decision", "node_id": "abc"}, ctx
        )
        assert result.is_error
        assert "content" in result.output.lower()

    @pytest.mark.asyncio
    async def test_unknown_action(self, tool, ctx):
        result = await tool.execute({"action": "invalid"}, ctx)
        assert result.is_error
