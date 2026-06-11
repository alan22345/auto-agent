"""Single source of truth for the MCP servers the agent connects to.

``build_mcp_servers(settings)`` assembles the list; each entry declares which
execution paths it applies to via ``targets`` (``"native"`` and/or ``"cli"``).
Adding a server later is a single entry here.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class McpServerSpec:
    """One MCP server, transport-tagged and path-targeted.

    ``targets`` is the set of execution paths this server applies to:
      - ``"native"`` — registered as Python tools in the native tool loop
        (HTTP transport only).
      - ``"cli"``    — serialized into the claude_cli ``--mcp-config``.
    """

    name: str
    transport: str  # "http" | "stdio"
    targets: frozenset[str]
    # http transport
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # stdio transport
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)

    def to_cli_entry(self) -> dict[str, Any]:
        """Return the Claude Code ``mcpServers[name]`` config dict."""
        if self.transport == "http":
            entry: dict[str, Any] = {"type": "http", "url": self.url}
            if self.headers:
                entry["headers"] = dict(self.headers)
            return entry
        # stdio
        entry = {"command": self.command, "args": list(self.args)}
        if self.env:
            entry["env"] = dict(self.env)
        return entry


def _resolve_team_memory_command() -> tuple[str, list[str]]:
    """Resolve a runnable ``team-memory serve`` for the *current* environment.

    Prefers the installed console script (present in the Dockerized VM, which
    installs team-memory from git); falls back to ``python -m team_memory.cli``
    (the module has an ``if __name__ == "__main__"`` guard) for environments
    where the script isn't on PATH.
    """
    exe = shutil.which("team-memory")
    if exe:
        return exe, ["serve"]
    return sys.executable, ["-m", "team_memory.cli", "serve"]


def build_mcp_servers(settings: Any, *, repo_id: int | None = None) -> list[McpServerSpec]:
    """Assemble the configured MCP servers. Returns [] when MCP is disabled.

    Entries whose required secret/config is missing are skipped (logged), so a
    half-configured deployment degrades to "fewer servers" rather than failing.

    ``repo_id`` pins the per-task code-graph server (ADR-023) to the repo the
    agent is working in; without it that server is omitted.
    """
    if not getattr(settings, "mcp_enabled", True):
        return []

    specs: list[McpServerSpec] = []

    # ergodic-ui — HTTP design system, used in both paths.
    url = (getattr(settings, "ergodic_ui_mcp_url", "") or "").strip()
    token = (getattr(settings, "ergodic_ui_mcp_token", "") or "").strip()
    if url and token:
        specs.append(
            McpServerSpec(
                name="ergodic-ui",
                transport="http",
                targets=frozenset({"native", "cli"}),
                url=url,
                headers={"Authorization": f"Bearer {token}"},
            )
        )
    elif url and not token:
        logger.info("mcp_server_skipped", server="ergodic-ui", reason="no token")

    # team-memory — stdio MCP. Native mode keeps its faster in-process tools, so
    # this targets the CLI path only (which can't see in-process Python tools).
    db_url = (getattr(settings, "team_memory_database_url", "") or "").strip()
    if db_url:
        command, args = _resolve_team_memory_command()
        specs.append(
            McpServerSpec(
                name="team-memory",
                transport="stdio",
                targets=frozenset({"cli"}),
                command=command,
                args=tuple(args),
                env={"TEAM_MEMORY_DATABASE_URL": db_url},
            )
        )
    else:
        logger.info("mcp_server_skipped", server="team-memory", reason="no db url")

    # code-graph — stdio MCP re-exposing query_repo_graph to the CLI path,
    # which can't see in-process Python tools (ADR-023). Native mode keeps
    # the in-process tool. Pinned to the task's repo; DATABASE_URL is
    # forwarded so the subprocess resolves the same Postgres regardless of
    # the CLI's working directory.
    if repo_id is not None:
        specs.append(
            McpServerSpec(
                name="code-graph",
                transport="stdio",
                targets=frozenset({"cli"}),
                command=sys.executable,
                args=("-m", "agent.mcp.code_graph_server"),
                env={
                    "CODE_GRAPH_REPO_ID": str(repo_id),
                    "DATABASE_URL": getattr(settings, "database_url", "") or "",
                },
            )
        )

    return specs


def native_http_specs(specs: list[McpServerSpec]) -> list[McpServerSpec]:
    """HTTP servers that apply to the native tool loop."""
    return [s for s in specs if "native" in s.targets and s.transport == "http"]


def cli_specs(specs: list[McpServerSpec]) -> list[McpServerSpec]:
    """Servers that apply to the claude_cli ``--mcp-config``."""
    return [s for s in specs if "cli" in s.targets]
