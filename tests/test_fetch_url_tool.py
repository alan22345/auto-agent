import json
from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.base import ToolContext
from agent.tools.fetch_url import FetchUrlTool

_HTML = """
<html><head><title>Hello World</title></head>
<body><h1>Hi</h1><p>Some <b>bold</b> text and a <a href="x">link</a>.</p>
<script>console.log('nope')</script>
</body></html>
"""


@pytest.mark.asyncio
async def test_fetch_url_returns_title_and_markdown():
    ctx = ToolContext(workspace="/tmp")
    tool = FetchUrlTool()
    with patch("agent.tools.fetch_url._http_get", new=AsyncMock(return_value=_HTML)):
        result = await tool.execute({"url": "https://example.com"}, ctx)
    assert not result.is_error
    payload = json.loads(result.output)
    assert payload["title"] == "Hello World"
    assert "Hi" in payload["content"]
    assert "console.log" not in payload["content"]


@pytest.mark.asyncio
async def test_fetch_url_truncates_long_content():
    ctx = ToolContext(workspace="/tmp")
    tool = FetchUrlTool(max_chars=200)
    big_html = "<html><body>" + ("x" * 5000) + "</body></html>"
    with patch("agent.tools.fetch_url._http_get", new=AsyncMock(return_value=big_html)):
        result = await tool.execute({"url": "https://example.com"}, ctx)
    payload = json.loads(result.output)
    assert len(payload["content"]) <= 220
    assert "truncated" in payload["content"].lower()


@pytest.mark.asyncio
async def test_fetch_url_handles_http_error():
    ctx = ToolContext(workspace="/tmp")
    tool = FetchUrlTool()
    with patch("agent.tools.fetch_url._http_get", new=AsyncMock(side_effect=RuntimeError("404"))):
        result = await tool.execute({"url": "https://example.com"}, ctx)
    assert result.is_error
    assert "404" in result.output
