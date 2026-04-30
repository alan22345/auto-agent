"""Brave Search API tool. Emits 'source' events as results arrive."""

from __future__ import annotations

import contextlib
import json
from typing import Any

import httpx
import structlog

from agent.tools.base import Tool, ToolContext, ToolResult

logger = structlog.get_logger()

_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


async def _brave_get(query: str, api_key: str, count: int) -> dict[str, Any]:
    """Call Brave Search and return the parsed JSON. Raises on HTTP error."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            _BRAVE_URL,
            params={"q": query, "count": count},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
        )
        resp.raise_for_status()
        return resp.json()


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web with Brave Search. Returns a list of results with "
        "url, title, and a short description (Brave's snippet). Use for "
        "current information beyond your training data."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query.",
            },
            "num_results": {
                "type": "integer",
                "description": "How many results to return (1-10). Default 6.",
                "default": 6,
            },
        },
        "required": ["query"],
    }
    is_readonly = True

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        query = (arguments.get("query") or "").strip()
        if not query:
            return ToolResult(output="Error: 'query' is required.", is_error=True)

        if not self._api_key:
            return ToolResult(
                output="Error: BRAVE_API_KEY is not configured on the server.",
                is_error=True,
            )

        count = max(1, min(10, int(arguments.get("num_results") or 6)))

        try:
            data = await _brave_get(query, self._api_key, count)
        except Exception as e:
            logger.warning("web_search_failed", error=str(e), query=query)
            return ToolResult(output=f"Error calling Brave Search: {e}", is_error=True)

        web = (data or {}).get("web") or {}
        raw_results = web.get("results") or []

        results: list[dict[str, str]] = []
        for r in raw_results[:count]:
            url = r.get("url") or ""
            if not url:
                continue
            item = {
                "url": url,
                "title": r.get("title") or url,
                "description": r.get("description") or "",
            }
            results.append(item)
            if context.event_sink is not None:
                with contextlib.suppress(Exception):
                    await context.event_sink({
                        "type": "source",
                        "url": item["url"],
                        "title": item["title"],
                        "summary": item["description"],
                        "query": query,
                    })

        return ToolResult(
            output=json.dumps({"query": query, "results": results}, ensure_ascii=False),
            token_estimate=sum(len(r["description"]) for r in results) // 4,
        )
