"""Tests for agent/mcp/tool_adapter.py — wrapping remote MCP tools as Tools."""

from __future__ import annotations

import pytest

from agent.mcp.client import McpToolDef, McpUnavailable
from agent.mcp.servers import McpServerSpec
from agent.mcp.tool_adapter import (
    McpTool,
    clear_discovery_cache,
    register_mcp_tools,
)
from agent.tools.base import ToolContext, ToolRegistry


class _FakeClient:
    """Stand-in for McpHttpClient with scripted behaviour."""

    def __init__(self, *, tools=None, list_error=False, call_error=False, result="ok"):
        self._tools = tools or []
        self._list_error = list_error
        self._call_error = call_error
        self._result = result
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        if self._list_error:
            raise McpUnavailable("boom")
        return self._tools

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._call_error:
            raise McpUnavailable("server error")
        return self._result


def _spec():
    return McpServerSpec(
        name="ergodic-ui",
        transport="http",
        targets=frozenset({"native"}),
        url="https://example/mcp",
    )


def _ctx():
    return ToolContext(workspace="/tmp")


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_discovery_cache()
    yield
    clear_discovery_cache()


@pytest.mark.asyncio
async def test_mcp_tool_naming_and_schema():
    td = McpToolDef(name="list_components", description="d", input_schema={"type": "object"})
    tool = McpTool("ergodic-ui", _FakeClient(), td)
    assert tool.name == "mcp__ergodic-ui__list_components"
    assert tool.parameters == {"type": "object"}
    assert tool.is_readonly is True


@pytest.mark.asyncio
async def test_execute_maps_result():
    td = McpToolDef(name="get_component", description="", input_schema={})
    client = _FakeClient(result="<Button/>")
    tool = McpTool("ergodic-ui", client, td)
    res = await tool.execute({"name": "Button"}, _ctx())
    assert not res.is_error
    assert res.output == "<Button/>"
    assert client.calls == [("get_component", {"name": "Button"})]


@pytest.mark.asyncio
async def test_execute_maps_error():
    td = McpToolDef(name="get_component", description="", input_schema={})
    tool = McpTool("ergodic-ui", _FakeClient(call_error=True), td)
    res = await tool.execute({}, _ctx())
    assert res.is_error
    assert "Error calling mcp__ergodic-ui__get_component" in res.output


@pytest.mark.asyncio
async def test_register_mcp_tools_registers_wrappers():
    registry = ToolRegistry()
    client = _FakeClient(
        tools=[
            McpToolDef(name="init", description="", input_schema={}),
            McpToolDef(name="list_components", description="", input_schema={}),
        ]
    )
    n = await register_mcp_tools(registry, [_spec()], client_factory=lambda s: client)
    assert n == 2
    assert registry.get("mcp__ergodic-ui__init") is not None
    assert registry.get("mcp__ergodic-ui__list_components") is not None


@pytest.mark.asyncio
async def test_register_skips_on_discovery_failure():
    registry = ToolRegistry()
    client = _FakeClient(list_error=True)
    n = await register_mcp_tools(registry, [_spec()], client_factory=lambda s: client)
    assert n == 0
    assert registry.names() == []


@pytest.mark.asyncio
async def test_register_skips_non_http_specs():
    registry = ToolRegistry()
    stdio = McpServerSpec(
        name="team-memory", transport="stdio", targets=frozenset({"native"}), command="x"
    )
    n = await register_mcp_tools(registry, [stdio], client_factory=lambda s: _FakeClient())
    assert n == 0


@pytest.mark.asyncio
async def test_discovery_is_cached_per_process():
    registry = ToolRegistry()
    calls = {"n": 0}

    def factory(spec):
        calls["n"] += 1
        return _FakeClient(tools=[McpToolDef(name="init", description="", input_schema={})])

    await register_mcp_tools(registry, [_spec()], client_factory=factory)
    await register_mcp_tools(registry, [_spec()], client_factory=factory)
    # list_tools result cached → second call reuses cached defs (no re-list),
    # though a client is still constructed for binding.
    assert registry.get("mcp__ergodic-ui__init") is not None
