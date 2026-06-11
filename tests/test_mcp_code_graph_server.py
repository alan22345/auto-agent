"""Tests for agent/mcp/code_graph_server.py — the stdio MCP bridge (ADR-023).

The claude_cli passthrough can't see in-process Python tools, so the
code graph is re-exposed as a stdio MCP server pinned to one repo via
``CODE_GRAPH_REPO_ID``. The server is a thin wrapper: every call goes
through the same :class:`QueryRepoGraphTool` the native loop uses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.mcp import code_graph_server
from agent.tools.base import ToolResult


@pytest.mark.asyncio
async def test_run_query_passes_pinned_repo_id_and_args(monkeypatch) -> None:
    monkeypatch.setenv("CODE_GRAPH_REPO_ID", "7")
    execute = AsyncMock(return_value=ToolResult(output='{"op": "search_symbols"}'))

    with patch.object(code_graph_server.QueryRepoGraphTool, "execute", execute):
        output = await code_graph_server.run_query("search_symbols", {"query": "parse"})

    assert output == '{"op": "search_symbols"}'
    (arguments, _context), _ = execute.call_args
    assert arguments == {
        "repo_id": 7,
        "op": "search_symbols",
        "params": {"query": "parse"},
    }


@pytest.mark.asyncio
async def test_run_query_defaults_params_to_empty_dict(monkeypatch) -> None:
    monkeypatch.setenv("CODE_GRAPH_REPO_ID", "7")
    execute = AsyncMock(return_value=ToolResult(output="{}"))

    with patch.object(code_graph_server.QueryRepoGraphTool, "execute", execute):
        await code_graph_server.run_query("hotspots", None)

    (arguments, _context), _ = execute.call_args
    assert arguments["params"] == {}


@pytest.mark.asyncio
async def test_run_query_raises_on_error_result(monkeypatch) -> None:
    monkeypatch.setenv("CODE_GRAPH_REPO_ID", "7")
    execute = AsyncMock(return_value=ToolResult(output="Error: unknown op 'zap'.", is_error=True))

    with (
        patch.object(code_graph_server.QueryRepoGraphTool, "execute", execute),
        pytest.raises(RuntimeError, match="unknown op"),
    ):
        await code_graph_server.run_query("zap", {})


@pytest.mark.asyncio
async def test_run_query_without_env_repo_id_raises(monkeypatch) -> None:
    monkeypatch.delenv("CODE_GRAPH_REPO_ID", raising=False)

    with pytest.raises(RuntimeError, match="CODE_GRAPH_REPO_ID"):
        await code_graph_server.run_query("hotspots", {})


@pytest.mark.asyncio
async def test_mcp_server_exposes_query_repo_graph_tool() -> None:
    tools = await code_graph_server.mcp.list_tools()
    assert any(t.name == "query_repo_graph" for t in tools)
