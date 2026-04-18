"""Memory read tool — search and traverse the shared graph memory."""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from agent.tools.base import Tool, ToolContext, ToolResult
from shared.database import async_session
from shared.models import MemoryEdge, MemoryNode

logger = structlog.get_logger()


async def _search_nodes(query: str, limit: int = 10) -> list[MemoryNode]:
    """Search nodes by name or content using ILIKE."""
    pattern = f"%{query}%"
    async with async_session() as session:
        result = await session.execute(
            select(MemoryNode)
            .where(or_(MemoryNode.name.ilike(pattern), MemoryNode.content.ilike(pattern)))
            .options(selectinload(MemoryNode.outgoing_edges), selectinload(MemoryNode.incoming_edges))
            .limit(limit)
        )
        return list(result.scalars().all())


async def _traverse_node(node_id: uuid.UUID, depth: int = 2) -> dict:
    """Traverse from a node, following edges up to N levels."""
    visited: set[uuid.UUID] = set()
    nodes: list[dict] = []
    edges: list[dict] = []

    async with async_session() as session:
        queue = [(node_id, 0)]
        while queue:
            current_id, current_depth = queue.pop(0)
            if current_id in visited or current_depth > depth:
                continue
            visited.add(current_id)

            result = await session.execute(
                select(MemoryNode)
                .where(MemoryNode.id == current_id)
                .options(selectinload(MemoryNode.outgoing_edges), selectinload(MemoryNode.incoming_edges))
            )
            node = result.scalar_one_or_none()
            if not node:
                continue

            nodes.append({
                "id": str(node.id),
                "name": node.name,
                "type": node.node_type,
                "content": node.content,
            })

            for edge in node.outgoing_edges:
                edges.append({
                    "source": str(edge.source_id),
                    "target": str(edge.target_id),
                    "relation": edge.relation,
                })
                if edge.target_id not in visited and current_depth + 1 <= depth:
                    queue.append((edge.target_id, current_depth + 1))

            for edge in node.incoming_edges:
                edges.append({
                    "source": str(edge.source_id),
                    "target": str(edge.target_id),
                    "relation": edge.relation,
                })
                if edge.source_id not in visited and current_depth + 1 <= depth:
                    queue.append((edge.source_id, current_depth + 1))

    return {"nodes": nodes, "edges": edges}


async def _get_decision_chain(node_id: uuid.UUID) -> list[dict]:
    """Follow evolved-from edges from newest to oldest."""
    chain: list[dict] = []
    visited: set[uuid.UUID] = set()
    current_id = node_id

    async with async_session() as session:
        while current_id and current_id not in visited:
            visited.add(current_id)
            result = await session.execute(
                select(MemoryNode)
                .where(MemoryNode.id == current_id)
                .options(selectinload(MemoryNode.outgoing_edges))
            )
            node = result.scalar_one_or_none()
            if not node:
                break

            chain.append({
                "id": str(node.id),
                "name": node.name,
                "type": node.node_type,
                "content": node.content,
                "created_at": node.created_at.isoformat() if node.created_at else None,
            })

            next_id = None
            for edge in node.outgoing_edges:
                if edge.relation == "evolved-from":
                    next_id = edge.target_id
                    break
            current_id = next_id

    return chain


async def _list_root_nodes() -> list[MemoryNode]:
    """Return all nodes with no incoming edges (root nodes)."""
    async with async_session() as session:
        has_incoming = select(MemoryEdge.target_id).distinct()
        result = await session.execute(
            select(MemoryNode).where(MemoryNode.id.notin_(has_incoming))
        )
        return list(result.scalars().all())


class MemoryReadTool(Tool):
    """Search and traverse the shared team graph memory."""

    name = "memory_read"
    description = (
        "Search and traverse the shared team graph memory. "
        "Use 'search' to find nodes by keyword, 'traverse' to explore connections, "
        "'get_decision_chain' to see decision history, 'list_roots' for top-level entries."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "traverse", "get_decision_chain", "list_roots"],
                "description": "The read operation to perform.",
            },
            "query": {
                "type": "string",
                "description": "Search query (required for 'search' action).",
            },
            "node_id": {
                "type": "string",
                "description": "UUID of the node (required for 'traverse' and 'get_decision_chain').",
            },
            "depth": {
                "type": "integer",
                "description": "Traversal depth (default 2, for 'traverse' action).",
            },
        },
        "required": ["action"],
    }
    is_readonly = True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        action = arguments["action"]

        try:
            if action == "search":
                query = arguments.get("query", "")
                if not query:
                    return ToolResult(output="Error: 'query' is required for search", is_error=True)
                nodes = await _search_nodes(query)
                if not nodes:
                    return ToolResult(output=f"No memory nodes found matching '{query}'")
                output_parts = []
                for node in nodes:
                    edges_str = ""
                    for e in node.outgoing_edges:
                        edges_str += f"\n  --[{e.relation}]--> {e.target_id}"
                    for e in node.incoming_edges:
                        edges_str += f"\n  <--[{e.relation}]-- {e.source_id}"
                    output_parts.append(
                        f"[{node.node_type}] {node.name} (id: {node.id})\n"
                        f"  Content: {node.content}{edges_str}"
                    )
                return ToolResult(output="\n\n".join(output_parts))

            elif action == "traverse":
                node_id_str = arguments.get("node_id", "")
                if not node_id_str:
                    return ToolResult(output="Error: 'node_id' is required for traverse", is_error=True)
                depth = arguments.get("depth", 2)
                result = await _traverse_node(uuid.UUID(node_id_str), depth)
                return ToolResult(output=json.dumps(result, indent=2))

            elif action == "get_decision_chain":
                node_id_str = arguments.get("node_id", "")
                if not node_id_str:
                    return ToolResult(output="Error: 'node_id' is required for get_decision_chain", is_error=True)
                chain = await _get_decision_chain(uuid.UUID(node_id_str))
                if not chain:
                    return ToolResult(output="No decision chain found for this node")
                output_parts = []
                for i, node in enumerate(chain):
                    prefix = "CURRENT" if i == 0 else f"v{len(chain) - i}"
                    output_parts.append(
                        f"[{prefix}] {node['name']} ({node['created_at']})\n  {node['content']}"
                    )
                return ToolResult(output="\n\n".join(output_parts))

            elif action == "list_roots":
                nodes = await _list_root_nodes()
                if not nodes:
                    return ToolResult(output="No root nodes in memory. The graph is empty.")
                output_parts = [
                    f"[{n.node_type}] {n.name} (id: {n.id})" for n in nodes
                ]
                return ToolResult(output="Root nodes:\n" + "\n".join(output_parts))

            else:
                return ToolResult(output=f"Unknown action: {action}", is_error=True)

        except Exception as e:
            logger.error("memory_read_error", action=action, error=str(e))
            return ToolResult(output=f"Error: {e}", is_error=True)
