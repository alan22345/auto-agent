import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.remember_memory import RememberMemoryTool


class _FakeSessionCtx:
    def __init__(self, session): self._session = session
    async def __aenter__(self): return self._session
    async def __aexit__(self, *a): return None


@pytest.mark.asyncio
async def test_remember_memory_writes_fact():
    ctx = ToolContext(workspace="/tmp")
    tool = RememberMemoryTool(author="alan@ergodic.ai")

    fake_session = MagicMock()
    fake_session.commit = AsyncMock()
    fake_engine = MagicMock()
    fake_engine.remember = AsyncMock(return_value={
        "entity_id": "e1", "fact_id": "f1", "created_entity": True,
    })

    with patch("agent.tools.remember_memory.team_memory_session", lambda: _FakeSessionCtx(fake_session)), \
         patch("agent.tools.remember_memory.GraphEngine", return_value=fake_engine):
        result = await tool.execute({
            "entity_name": "Alan",
            "entity_type": "person",
            "fact": "Prefers terse responses.",
            "kind": "preference",
        }, ctx)

    assert not result.is_error
    payload = json.loads(result.output)
    assert payload["fact_id"] == "f1"
    fake_engine.remember.assert_awaited_once()
    fake_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_remember_memory_session_unavailable():
    ctx = ToolContext(workspace="/tmp")
    tool = RememberMemoryTool()
    with patch("agent.tools.remember_memory.team_memory_session", None):
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
