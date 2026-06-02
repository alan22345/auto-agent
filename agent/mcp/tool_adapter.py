"""Bridge remote MCP tools into the agent's ToolRegistry.

``register_mcp_tools`` discovers each native HTTP server's tools and registers
an ``McpTool`` wrapper per tool. Discovery is cached per-process so repeated
tasks pay no network cost, and a down server is logged-and-skipped — it never
blocks a task.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from agent.mcp.client import McpHttpClient, McpToolDef, McpUnavailable
from agent.tools.base import Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent.mcp.servers import McpServerSpec

logger = structlog.get_logger()

# Per-process discovery cache, keyed by (server name, url). Tool catalogues
# rarely change, so we fetch each server's tools once per process.
_discovery_cache: dict[tuple[str, str | None], list[McpToolDef]] = {}


def clear_discovery_cache() -> None:
    """Reset the per-process discovery cache (used by tests)."""
    _discovery_cache.clear()


class McpTool(Tool):
    """Adapts one remote MCP tool to the agent's Tool interface."""

    def __init__(self, server_name: str, client: McpHttpClient, tool_def: McpToolDef):
        self.name = f"mcp__{server_name}__{tool_def.name}"
        self.description = tool_def.description or f"{server_name} MCP tool {tool_def.name}"
        self.parameters = tool_def.input_schema or {"type": "object", "properties": {}}
        self.is_readonly = True
        self._client = client
        self._remote_name = tool_def.name

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            text = await self._client.call_tool(self._remote_name, arguments)
        except McpUnavailable as e:
            logger.warning("mcp_tool_call_failed", tool=self.name, error=str(e))
            return ToolResult(output=f"Error calling {self.name}: {e}", is_error=True)
        return ToolResult(output=text, token_estimate=len(text) // 4)


def _default_client_factory(spec: McpServerSpec) -> McpHttpClient:
    return McpHttpClient(spec.url or "", headers=spec.headers)


async def register_mcp_tools(
    registry: Any,
    native_specs: list[McpServerSpec],
    *,
    client_factory: Callable[[McpServerSpec], Any] | None = None,
) -> int:
    """Register MCP tools from each HTTP server into ``registry``.

    Returns the number of tools registered. Idempotent — re-registering the
    same tool name overwrites. Discovery failures are logged and skipped.
    """
    factory = client_factory or _default_client_factory
    count = 0
    for spec in native_specs:
        if spec.transport != "http":
            continue
        client = factory(spec)
        key = (spec.name, spec.url)
        defs = _discovery_cache.get(key)
        if defs is None:
            try:
                defs = await client.list_tools()
            except McpUnavailable as e:
                logger.warning("mcp_discovery_failed", server=spec.name, error=str(e))
                continue
            _discovery_cache[key] = defs
        for td in defs:
            registry.register(McpTool(spec.name, client, td))
            count += 1
        logger.info("mcp_tools_registered", server=spec.name, count=len(defs))
    return count
