"""Tests for agent/mcp/servers.py — the MCP server config source of truth."""

from __future__ import annotations

from types import SimpleNamespace

from agent.mcp.servers import (
    build_mcp_servers,
    cli_specs,
    native_http_specs,
)


def _settings(**over):
    base = dict(
        mcp_enabled=True,
        ergodic_ui_mcp_url="https://ergodic-ui-mcp.fly.dev/mcp",
        ergodic_ui_mcp_token="tok-123",
        team_memory_database_url="postgresql+asyncpg://u:p@host/db",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_full_config_yields_both_servers():
    specs = build_mcp_servers(_settings())
    by_name = {s.name: s for s in specs}
    assert set(by_name) == {"ergodic-ui", "team-memory"}

    ergodic = by_name["ergodic-ui"]
    assert ergodic.transport == "http"
    assert ergodic.targets == frozenset({"native", "cli"})
    assert ergodic.headers["Authorization"] == "Bearer tok-123"

    tm = by_name["team-memory"]
    assert tm.transport == "stdio"
    assert tm.targets == frozenset({"cli"})  # native keeps in-process tools
    assert tm.env["TEAM_MEMORY_DATABASE_URL"].endswith("/db")
    assert "serve" in tm.args


def test_disabled_returns_empty():
    assert build_mcp_servers(_settings(mcp_enabled=False)) == []


def test_ergodic_skipped_without_token():
    specs = build_mcp_servers(_settings(ergodic_ui_mcp_token=""))
    assert [s.name for s in specs] == ["team-memory"]


def test_team_memory_skipped_without_db_url():
    specs = build_mcp_servers(_settings(team_memory_database_url=""))
    assert [s.name for s in specs] == ["ergodic-ui"]


def test_targets_filters():
    specs = build_mcp_servers(_settings())
    assert [s.name for s in native_http_specs(specs)] == ["ergodic-ui"]
    assert sorted(s.name for s in cli_specs(specs)) == ["ergodic-ui", "team-memory"]


def test_to_cli_entry_http_shape():
    specs = build_mcp_servers(_settings())
    ergodic = next(s for s in specs if s.name == "ergodic-ui")
    entry = ergodic.to_cli_entry()
    assert entry == {
        "type": "http",
        "url": "https://ergodic-ui-mcp.fly.dev/mcp",
        "headers": {"Authorization": "Bearer tok-123"},
    }


def test_to_cli_entry_stdio_shape():
    specs = build_mcp_servers(_settings())
    tm = next(s for s in specs if s.name == "team-memory")
    entry = tm.to_cli_entry()
    assert entry["command"]
    assert "serve" in entry["args"]
    assert entry["env"]["TEAM_MEMORY_DATABASE_URL"].endswith("/db")
    assert "type" not in entry  # stdio entries have no "type"


def test_code_graph_added_when_repo_id_given():
    specs = build_mcp_servers(
        _settings(database_url="postgresql+asyncpg://u:p@host/app"), repo_id=7
    )
    cg = next(s for s in specs if s.name == "code-graph")
    assert cg.transport == "stdio"
    assert cg.targets == frozenset({"cli"})  # native keeps the in-process tool
    assert cg.env["CODE_GRAPH_REPO_ID"] == "7"
    assert cg.env["DATABASE_URL"].endswith("/app")
    assert cg.args[-1] == "agent.mcp.code_graph_server"


def test_code_graph_absent_without_repo_id():
    specs = build_mcp_servers(_settings(database_url="postgresql+asyncpg://u:p@host/app"))
    assert "code-graph" not in {s.name for s in specs}
