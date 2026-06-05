"""Tests for claude_cli MCP wiring — --mcp-config serialization."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

from agent.llm.claude_cli import ClaudeCLIProvider
from agent.mcp.servers import build_mcp_servers, cli_specs


def _settings(**over):
    base = dict(
        mcp_enabled=True,
        ergodic_ui_mcp_url="https://ergodic-ui-mcp.fly.dev/mcp",
        ergodic_ui_mcp_token="tok-123",
        team_memory_database_url="postgresql+asyncpg://u:p@host/db",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_no_servers_means_no_flags():
    p = ClaudeCLIProvider()
    args, path = p._mcp_config_args()
    assert args == []
    assert path is None


def test_mcp_config_contains_both_servers():
    p = ClaudeCLIProvider()
    p.set_mcp_servers(cli_specs(build_mcp_servers(_settings())))
    args, path = p._mcp_config_args()
    try:
        assert args[0] == "--mcp-config"
        assert args[1] == path
        assert "--strict-mcp-config" in args

        with open(path) as f:
            config = json.load(f)
        servers = config["mcpServers"]
        assert set(servers) == {"ergodic-ui", "team-memory"}
        assert servers["ergodic-ui"]["type"] == "http"
        assert servers["ergodic-ui"]["headers"]["Authorization"] == "Bearer tok-123"
        assert "serve" in servers["team-memory"]["args"]
        assert servers["team-memory"]["env"]["TEAM_MEMORY_DATABASE_URL"].endswith("/db")
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def test_set_empty_servers_disables():
    p = ClaudeCLIProvider()
    p.set_mcp_servers(cli_specs(build_mcp_servers(_settings(mcp_enabled=False))))
    args, path = p._mcp_config_args()
    assert args == []
    assert path is None
