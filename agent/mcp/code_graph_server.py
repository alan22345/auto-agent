"""Stdio MCP server exposing the code graph to Claude Code CLI (ADR-023).

The claude_cli passthrough hands the whole task to ``claude --print``,
which only sees tools reachable over MCP — in-process Python tools like
:class:`QueryRepoGraphTool` are invisible on that path. This server
closes the gap: it wraps the exact same tool behind one MCP tool, so
both execution paths query the graph through identical code.

The server is pinned to a single repo via the ``CODE_GRAPH_REPO_ID``
environment variable, set by :func:`agent.mcp.servers.build_mcp_servers`
when it assembles the per-task ``--mcp-config``. ``DATABASE_URL`` is
forwarded the same way so :mod:`shared.config` resolves the right
Postgres regardless of the CLI's working directory.

Run with ``python -m agent.mcp.code_graph_server`` (stdio transport).
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from agent.tools.base import ToolContext
from agent.tools.query_repo_graph import QueryRepoGraphTool

mcp = FastMCP("code-graph")


def _pinned_repo_id() -> int:
    """Repo this server instance serves, from ``CODE_GRAPH_REPO_ID``."""
    raw = os.environ.get("CODE_GRAPH_REPO_ID", "")
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(
            "CODE_GRAPH_REPO_ID must be set to the numeric repo id "
            f"(got '{raw}') — this server is spawned per task by "
            "agent.mcp.servers.build_mcp_servers."
        ) from None


async def run_query(op: str, params: dict | None) -> str:
    """Run one graph query through the shared tool; raise on error results.

    Raising (rather than returning the error text) lets FastMCP mark the
    response ``isError`` so the calling agent treats it as a failure.
    """
    tool = QueryRepoGraphTool()
    result = await tool.execute(
        {"repo_id": _pinned_repo_id(), "op": op, "params": params or {}},
        ToolContext(workspace=os.getcwd()),
    )
    if result.is_error:
        raise RuntimeError(result.output)
    return result.output


@mcp.tool()
async def query_repo_graph(op: str, params: dict | None = None) -> str:
    """Query this repo's pre-analysed code graph for structural facts.

    Prefer this over grep and whole-file reads — it's exact, pre-indexed,
    and much cheaper on context. The navigation loop: ``search_symbols``
    turns a name into node ids, relationship ops explore from there, and
    ``get_symbol_source`` reads just the symbol's body. Before writing a
    new helper, call ``search_symbols`` first — an equivalent may exist.

    Ops (pass op-specific arguments in ``params``):
    - search_symbols {query, kind?, area?, limit?=20} — find node ids by name
    - get_symbol_source {node_id, context_lines?=0} — one symbol's source
    - callers_of / callees_of {node_id}
    - outgoing_edges / incoming_edges {node_id}
    - public_surface {area_name}
    - path_between {source_id, target_id, max_depth?=5}
    - violates_boundaries {source_id, target_id}
    - which_capability {node}
    - cycles_for {node_id}
    - hotspots {limit?=20} / clones {min_tokens?=0}
    - dead_code {kind?} / complex_functions {metric, threshold}
    - file_health {band?}

    Node ids look like ``path/to/file.py::ClassName.method_name``. Every
    response carries ``staleness.drifted`` and per-result
    ``exists_in_workspace`` flags — trust accordingly.
    """
    return await run_query(op, params)


if __name__ == "__main__":
    mcp.run()
