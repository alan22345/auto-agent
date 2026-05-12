"""Agent-callable visual-capture tool — Playwright headless screenshot."""
from __future__ import annotations

import base64
import json
import re
from typing import Any, ClassVar

import structlog
from playwright.async_api import async_playwright

from agent.tools.base import Tool, ToolContext, ToolResult

logger = structlog.get_logger()

_TEXT_CAP = 5000


def _html_to_text(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class BrowseUrlTool(Tool):
    name = "browse_url"
    description = (
        "Navigate to a URL with a headless browser, return rendered text and a "
        "full-page PNG screenshot. Use this to inspect the running app while you "
        "work, or to check whether a route renders as the task expects."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "wait_for": {"type": "string", "default": "body"},
            "viewport": {
                "type": "object",
                "properties": {
                    "width": {"type": "integer", "default": 1280},
                    "height": {"type": "integer", "default": 800},
                },
            },
        },
        "required": ["url"],
    }
    is_readonly = True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        url = arguments["url"]
        wait_for = arguments.get("wait_for") or "body"
        vp = arguments.get("viewport") or {}
        viewport = {"width": vp.get("width", 1280), "height": vp.get("height", 800)}

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context_obj = await browser.new_context(viewport=viewport)
                page = await context_obj.new_page()
                try:
                    response = await page.goto(url, wait_until="networkidle", timeout=30_000)
                    status = response.status if response else 0
                    final_url = response.url if response else url
                    try:
                        await page.wait_for_selector(wait_for, timeout=15_000)
                    except Exception:
                        logger.debug("browse_url_selector_timeout", url=url, selector=wait_for)
                    png_bytes = await page.screenshot(full_page=True)
                    html = await page.content()
                finally:
                    await context_obj.close()
                    await browser.close()
        except Exception as e:
            logger.warning("browse_url_failed", error=str(e), url=url)
            return ToolResult(output=f"browse_url failed: {e}", is_error=True)

        text = _html_to_text(html)[:_TEXT_CAP]
        # Pack a structured payload into the single ToolResult.output for now.
        # See # TODO(browse_url) below — agent loop should emit native image blocks.
        payload = {
            "http_status": status,
            "final_url": final_url,
            "text": text,
            "screenshot_base64": base64.b64encode(png_bytes).decode("ascii"),
            "screenshot_media_type": "image/png",
        }
        # TODO(browse_url): emit native Anthropic image content blocks once
        # ToolResult supports them; for now we serialise the screenshot inline.
        return ToolResult(output=json.dumps(payload))
