"""External MCP (Model Context Protocol) server integration.

A single config-driven list of servers (``servers.build_mcp_servers``) feeds
both execution paths:

- Native tool loop (bedrock/anthropic): ``tool_adapter.register_mcp_tools``
  discovers each HTTP server's tools and registers them in the ToolRegistry.
- claude_cli pass-through: the same specs are serialized into a
  ``--mcp-config`` file by ``agent/llm/claude_cli.py``.

See docs/superpowers/specs/2026-06-02-pluggable-mcp-design.md.
"""

from __future__ import annotations

from agent.mcp.servers import McpServerSpec, build_mcp_servers
from agent.mcp.tool_adapter import McpTool, register_mcp_tools

__all__ = [
    "McpServerSpec",
    "McpTool",
    "build_mcp_servers",
    "register_mcp_tools",
]
