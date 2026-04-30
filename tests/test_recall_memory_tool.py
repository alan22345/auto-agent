import json
from unittest.mock import AsyncMock, MagicMock, patch

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


class _FakeSessionCtx:
    def __init__(self, session): self._session = session
    async def __aenter__(self): return self._session
    async def __aexit__(self, *a): return None


@pytest.mark.asyncio
async def test_recall_memory_returns_matches_and_emits_events():
    received: list[dict] = []

    async def sink(event: dict) -> None:
        received.append(event)

    ctx = ToolContext(workspace="/tmp", event_sink=sink)
    tool = RecallMemoryTool()

    fake_session = MagicMock()
    fake_engine = MagicMock()
    fake_engine.recall = AsyncMock(return_value=_RECALL_RESULT)

    with patch("agent.tools.recall_memory.team_memory_session", lambda: _FakeSessionCtx(fake_session)), \
         patch("agent.tools.recall_memory.GraphEngine", return_value=fake_engine):
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
    with patch("agent.tools.recall_memory.team_memory_session", None):
        result = await tool.execute({"query": "x"}, ctx)
    assert result.is_error
    assert "team-memory" in result.output.lower()
