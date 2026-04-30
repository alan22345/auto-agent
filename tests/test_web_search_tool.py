import json
from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.web_search import WebSearchTool

_BRAVE_RESPONSE = {
    "web": {
        "results": [
            {
                "url": "https://example.com/a",
                "title": "Example A",
                "description": "A description.",
            },
            {
                "url": "https://example.com/b",
                "title": "Example B",
                "description": "B description.",
            },
        ]
    }
}


@pytest.mark.asyncio
async def test_web_search_returns_results_and_emits_source_events():
    received: list[dict] = []

    async def sink(event: dict) -> None:
        received.append(event)

    ctx = ToolContext(workspace="/tmp", event_sink=sink)
    tool = WebSearchTool(api_key="fake")

    with patch("agent.tools.web_search._brave_get", new=AsyncMock(return_value=_BRAVE_RESPONSE)):
        result = await tool.execute({"query": "alpha", "num_results": 2}, ctx)

    assert not result.is_error
    payload = json.loads(result.output)
    assert len(payload["results"]) == 2
    assert payload["results"][0]["url"] == "https://example.com/a"
    assert payload["results"][0]["title"] == "Example A"
    assert [e["type"] for e in received] == ["source", "source"]
    assert received[0]["url"] == "https://example.com/a"
    assert received[0]["query"] == "alpha"


@pytest.mark.asyncio
async def test_web_search_missing_api_key_returns_error():
    ctx = ToolContext(workspace="/tmp")
    tool = WebSearchTool(api_key="")
    result = await tool.execute({"query": "x"}, ctx)
    assert result.is_error
    assert "BRAVE_API_KEY" in result.output


@pytest.mark.asyncio
async def test_web_search_handles_brave_failure():
    ctx = ToolContext(workspace="/tmp")
    tool = WebSearchTool(api_key="fake")
    with patch("agent.tools.web_search._brave_get", new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await tool.execute({"query": "x"}, ctx)
    assert result.is_error
    assert "boom" in result.output
