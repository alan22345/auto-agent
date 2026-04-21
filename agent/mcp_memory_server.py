"""MCP server exposing graph memory tools to Claude Code CLI.

Run via: python -m agent.mcp_memory_server
Configured in .claude/settings.local.json as an MCP server.
"""

from __future__ import annotations

import json
import uuid as _uuid

from mcp.server.fastmcp import FastMCP

from agent.tools.memory_read import (
    _get_decision_chain,
    _list_root_nodes,
    _search_nodes,
    _traverse_node,
)
from shared.database import async_session
from shared.models import MemoryEdge, MemoryNode

mcp = FastMCP(
    "auto-agent-memory",
    instructions=(
        "Graph memory for the auto-agent team. Use memory_search to find existing "
        "knowledge before creating new nodes. Use memory_create_node to record "
        "decisions, capabilities, and preferences learned during tasks."
    ),
)


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_search(query: str, limit: int = 10) -> str:
    """Search the team knowledge graph by keyword. Returns matching nodes with their edges."""
    nodes = await _search_nodes(query, limit)
    if not nodes:
        return f"No memory nodes found matching '{query}'"
    parts = []
    for node in nodes:
        edges_str = ""
        for e in node.outgoing_edges:
            edges_str += f"\n  --[{e.relation}]--> {e.target_id}"
        for e in node.incoming_edges:
            edges_str += f"\n  <--[{e.relation}]-- {e.source_id}"
        parts.append(
            f"[{node.node_type}] {node.name} (id: {node.id})\n"
            f"  Content: {node.content}{edges_str}"
        )
    return "\n\n".join(parts)


@mcp.tool()
async def memory_traverse(node_id: str, depth: int = 2) -> str:
    """Traverse the graph from a node, following edges up to N levels deep."""
    result = await _traverse_node(_uuid.UUID(node_id), depth)
    return json.dumps(result, indent=2)


@mcp.tool()
async def memory_get_decision_chain(node_id: str) -> str:
    """Follow the evolved-from chain for a decision node, from newest to oldest."""
    chain = await _get_decision_chain(_uuid.UUID(node_id))
    if not chain:
        return "No decision chain found for this node"
    parts = []
    for i, node in enumerate(chain):
        prefix = "CURRENT" if i == 0 else f"v{len(chain) - i}"
        parts.append(f"[{prefix}] {node['name']} ({node['created_at']})\n  {node['content']}")
    return "\n\n".join(parts)


@mcp.tool()
async def memory_list_roots() -> str:
    """List all top-level nodes in the knowledge graph (nodes with no incoming edges)."""
    nodes = await _list_root_nodes()
    if not nodes:
        return "No root nodes in memory. The graph is empty."
    parts = [f"[{n.node_type}] {n.name} (id: {n.id})" for n in nodes]
    return "Root nodes:\n" + "\n".join(parts)


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


async def _create_node_db(
    name: str,
    node_type: str,
    content: str,
    task_id: int | None,
) -> MemoryNode:
    """Create a memory node in the database."""
    node = MemoryNode(name=name, node_type=node_type, content=content, created_by_task_id=task_id)
    async with async_session() as session:
        session.add(node)
        await session.commit()
        await session.refresh(node)
        return node


@mcp.tool()
async def memory_create_node(
    name: str,
    node_type: str,
    content: str,
    task_id: int | None = None,
) -> str:
    """Create a new knowledge node. Types: project, preference, decision, capability."""
    node = await _create_node_db(name, node_type, content, task_id)
    return f"Created node '{name}' (type: {node_type}, id: {node.id})"


@mcp.tool()
async def memory_create_edge(
    source_id: str,
    target_id: str,
    relation: str,
) -> str:
    """Link two nodes with a labeled edge (e.g., 'evolved-from', 'has-preference')."""
    edge = MemoryEdge(
        source_id=_uuid.UUID(source_id),
        target_id=_uuid.UUID(target_id),
        relation=relation,
    )
    async with async_session() as session:
        session.add(edge)
        await session.commit()
    return f"Created edge: {source_id} --[{relation}]--> {target_id}"


@mcp.tool()
async def memory_append_decision(
    node_id: str,
    content: str,
    task_id: int | None = None,
) -> str:
    """Append a new version to a decision chain, preserving the history via evolved-from edge."""
    from sqlalchemy import select as _select

    uid = _uuid.UUID(node_id)
    async with async_session() as session:
        result = await session.execute(_select(MemoryNode).where(MemoryNode.id == uid))
        parent = result.scalar_one_or_none()
        if not parent:
            return f"Error: node {node_id} not found"

        new_node = MemoryNode(
            name=parent.name,
            node_type="decision",
            content=content,
            created_by_task_id=task_id,
        )
        session.add(new_node)
        await session.flush()

        edge = MemoryEdge(source_id=new_node.id, target_id=uid, relation="evolved-from")
        session.add(edge)
        await session.commit()

        return f"Appended decision to '{parent.name}' chain (new id: {new_node.id})"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
