import json
from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.recall_memory import RecallMemoryTool

_RECALL_RESULT = {
    "ambiguous": False,
    "matches": [
        {
            "entity": {"id": "e1", "name": "Auto-Agent", "type": "project", "tags": []},
            "facts": [
                {"id": "f1", "content": "Personal project.", "kind": "decision",
                 "valid_from": None, "valid_until": None, "source": None},
            ],
            "relevance": 1.0,
        }
    ],
}


@pytest.mark.asyncio
async def test_recall_memory_returns_matches_and_emits_events():
    received: list[dict] = []

    async def sink(event: dict) -> None:
        received.append(event)

    ctx = ToolContext(workspace="/tmp", event_sink=sink)
    tool = RecallMemoryTool()

    with patch("shared.memory_client.configured", return_value=True), \
         patch("shared.memory_client.recall", AsyncMock(return_value=_RECALL_RESULT)):
        result = await tool.execute({"query": "auto-agent"}, ctx)

    assert not result.is_error
    payload = json.loads(result.output)
    assert payload["matches"][0]["entity"]["name"] == "Auto-Agent"
    assert [e["type"] for e in received] == ["memory_hit"]
    assert received[0]["entity"]["name"] == "Auto-Agent"
    assert received[0]["facts"][0]["content"] == "Personal project."


@pytest.mark.asyncio
async def test_recall_memory_session_unavailable():
    ctx = ToolContext(workspace="/tmp")
    tool = RecallMemoryTool()
    with patch("shared.memory_client.configured", return_value=False):
        result = await tool.execute({"query": "x"}, ctx)
    assert result.is_error
    assert "team-memory" in result.output.lower()
