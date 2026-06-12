import json
from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.remember_memory import RememberMemoryTool


@pytest.mark.asyncio
async def test_remember_memory_writes_fact():
    ctx = ToolContext(workspace="/tmp")
    tool = RememberMemoryTool(author="alan@ergodic.ai")

    remember = AsyncMock(return_value={"entity_id": "e1", "fact_id": "f1", "created_entity": True})
    with patch("shared.memory_client.configured", return_value=True), \
         patch("shared.memory_client.remember", remember):
        result = await tool.execute({
            "entity_name": "Alan",
            "entity_type": "person",
            "fact": "Prefers terse responses.",
            "kind": "preference",
        }, ctx)

    assert not result.is_error
    payload = json.loads(result.output)
    assert payload["fact_id"] == "f1"
    remember.assert_awaited_once()


@pytest.mark.asyncio
async def test_remember_memory_session_unavailable():
    ctx = ToolContext(workspace="/tmp")
    tool = RememberMemoryTool()
    with patch("shared.memory_client.configured", return_value=False):
        result = await tool.execute({
            "entity_name": "Alan", "entity_type": "person",
            "fact": "x", "kind": "preference",
        }, ctx)
    assert result.is_error


@pytest.mark.asyncio
async def test_remember_memory_validates_required_fields():
    ctx = ToolContext(workspace="/tmp")
    tool = RememberMemoryTool()
    result = await tool.execute({"entity_name": "Alan"}, ctx)
    assert result.is_error
    assert "fact" in result.output.lower() or "required" in result.output.lower()
