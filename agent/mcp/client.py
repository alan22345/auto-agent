"""Minimal HTTP MCP client over the official ``mcp`` SDK.

Opens a fresh Streamable-HTTP session per operation. These are infrequent
design-system reads, so a per-call session is simpler and safer than managing
a long-lived connection across the agent's lifecycle. Every failure surfaces as
``McpUnavailable`` so callers can degrade gracefully.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger()


class McpUnavailable(Exception):  # noqa: N818 — matches codebase convention (e.g. QuotaExceeded)
    """Raised when an MCP server can't be reached or a call fails."""


@dataclass
class McpToolDef:
    """A tool advertised by a remote MCP server."""

    name: str
    description: str
    input_schema: dict[str, Any]


def _content_to_text(result: Any) -> str:
    """Flatten an MCP CallToolResult's content blocks into plain text."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(str(getattr(block, "data", block)))
    return "\n".join(parts)


class McpHttpClient:
    """Stateless-per-call client for one HTTP MCP server."""

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 30.0):
        self._url = url
        self._headers = dict(headers or {})
        self._timeout = timeout

    async def list_tools(self) -> list[McpToolDef]:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(self._url, headers=self._headers) as (
                read,
                write,
                _,
            ), ClientSession(read, write) as session:
                await session.initialize()
                resp = await session.list_tools()
                return [
                    McpToolDef(
                        name=t.name,
                        description=t.description or "",
                        input_schema=dict(t.inputSchema or {}),
                    )
                    for t in resp.tools
                ]
        except Exception as e:
            raise McpUnavailable(f"list_tools failed for {self._url}: {e}") from e

    async def call_tool(self, name: str, arguments: dict[str, Any] | None) -> str:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            async with streamablehttp_client(self._url, headers=self._headers) as (
                read,
                write,
                _,
            ), ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments or {})
                text = _content_to_text(result)
                if getattr(result, "isError", False):
                    raise McpUnavailable(f"tool {name} returned an error: {text}")
                return text
        except McpUnavailable:
            raise
        except Exception as e:
            raise McpUnavailable(f"call_tool {name} failed for {self._url}: {e}") from e
