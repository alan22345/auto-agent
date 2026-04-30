"""Fetch a URL and return its main text content as markdown."""

from __future__ import annotations

import json
from typing import Any

import html2text
import httpx
import structlog
from bs4 import BeautifulSoup

from agent.tools.base import Tool, ToolContext, ToolResult

logger = structlog.get_logger()

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


async def _http_get(url: str, timeout: float = 15.0) -> str:
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


class FetchUrlTool(Tool):
    name = "fetch_url"
    description = (
        "Fetch a URL and return its main text content as markdown. "
        "Use when web_search snippets are insufficient and you need the "
        "full page content to answer."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute URL to fetch."}
        },
        "required": ["url"],
    }
    is_readonly = True

    def __init__(self, max_chars: int = 32_000) -> None:
        self._max_chars = max_chars

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        url = (arguments.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(output="Error: 'url' must be http(s)://...", is_error=True)

        try:
            html = await _http_get(url)
        except Exception as e:
            logger.warning("fetch_url_failed", error=str(e), url=url)
            return ToolResult(output=f"Error fetching URL: {e}", is_error=True)

        soup = BeautifulSoup(html, "lxml")
        title_tag = soup.find("title")
        title = (title_tag.get_text(strip=True) if title_tag else "") or url

        for tag in soup(["script", "style", "noscript", "iframe", "header", "footer", "nav"]):
            tag.decompose()

        converter = html2text.HTML2Text()
        converter.ignore_images = True
        converter.ignore_links = False
        converter.body_width = 0
        markdown = converter.handle(str(soup)).strip()

        if len(markdown) > self._max_chars:
            suffix = "\n\n[content truncated]"
            markdown = markdown[: self._max_chars - len(suffix)] + suffix

        return ToolResult(
            output=json.dumps({"url": url, "title": title, "content": markdown}, ensure_ascii=False),
            token_estimate=len(markdown) // 4,
        )
