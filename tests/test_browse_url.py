"""Tests for BrowseUrlTool."""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.tools.browse_url import BrowseUrlTool


@pytest.fixture
def mock_playwright():
    """Mock the async_playwright context manager and the headless Chromium chain."""
    with patch("agent.tools.browse_url.async_playwright") as ap:
        # Create the chain: async_playwright() -> __aenter__ -> .chromium.launch() -> browser
        # -> .new_context() -> context -> .new_page() -> page
        page = MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(status=200, url="http://x/"))
        page.wait_for_selector = AsyncMock()
        page.screenshot = AsyncMock(return_value=b"PNGBYTES")
        page.content = AsyncMock(return_value="<html><body>Hello world</body></html>")

        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()

        chromium = MagicMock()
        chromium.launch = AsyncMock(return_value=browser)

        pw = MagicMock(chromium=chromium)

        # async_playwright() returns an async context manager
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=pw)
        cm.__aexit__ = AsyncMock(return_value=None)
        ap.return_value = cm

        yield page  # tests can mutate page.content/screenshot if they need to


async def test_returns_image_block(mock_playwright):
    tool = BrowseUrlTool()
    result = await tool.execute({"url": "http://localhost:3000/"}, context=None)
    assert result.output is not None
    payload = json.loads(result.output)
    # The screenshot bytes (b"PNGBYTES") should be base64-encoded in the payload
    encoded = base64.b64encode(b"PNGBYTES").decode("ascii")
    assert payload["screenshot_base64"] == encoded
    assert payload["screenshot_media_type"] == "image/png"
    assert payload["http_status"] == 200


async def test_text_capped_at_5000(mock_playwright):
    mock_playwright.content = AsyncMock(return_value="x" * 20000)
    tool = BrowseUrlTool()
    result = await tool.execute({"url": "http://localhost:3000/"}, context=None)
    payload = json.loads(result.output)
    # The rendered text should be capped at ~5000 chars
    assert len(payload["text"]) <= 5100


async def test_returns_error_on_playwright_failure(mock_playwright):
    """Playwright failure must produce is_error=True so loop.py flags it correctly."""
    mock_playwright.goto = AsyncMock(side_effect=Exception("navigation timeout"))
    tool = BrowseUrlTool()
    result = await tool.execute({"url": "http://localhost:3000/"}, context=None)
    assert result.is_error is True
    assert "navigation timeout" in result.output
