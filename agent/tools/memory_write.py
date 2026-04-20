"""Memory write tool — create, update, and delete nodes and edges in graph memory."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import delete, select

from agent.tools.base import Tool, ToolContext, ToolResult
from shared.database import async_session
from shared.models import MemoryEdge, MemoryNode

logger = structlog.get_logger()


class MemoryWriteTool(Tool):
    """Modify the shared graph memory — create nodes/edges, append decisions, update or delete."""

    name = "memory_write"
    description = (
        "Modify the shared team graph memory. "
        "Use 'create_node' to add knowledge, 'create_edge' to link nodes, "
        "'append_decision' to add to a decision chain (preserving history), "
        "'update_node' to amend content (corrections only), 'delete_node' to remove."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create_node", "create_edge", "append_decision", "update_node", "delete_node"],
                "description": "The write operation to perform.",
            },
            "name": {
                "type": "string",
                "description": "Node name (for create_node).",
            },
            "node_type": {
                "type": "string",
                "description": "Node type (for create_node), e.g. 'project', 'preference', 'decision', 'capability'.",
            },
            "content": {
                "type": "string",
                "description": "Node content (for create_node, append_decision, update_node).",
            },
            "node_id": {
                "type": "string",
                "description": "UUID of existing node (for update_node, delete_node, append_decision).",
            },
            "source_id": {
                "type": "string",
                "description": "Source node UUID (for create_edge).",
            },
            "target_id": {
                "type": "string",
                "description": "Target node UUID (for create_edge).",
            },
            "relation": {
                "type": "string",
                "description": "Edge relation label (for create_edge, append_decision). Default 'evolved-from' for append_decision.",
            },
            "task_id": {
                "type": "integer",
                "description": "Task ID that triggered this write (optional, for attribution).",
            },
        },
        "required": ["action"],
    }
    is_readonly = False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        action = arguments["action"]
        task_id = arguments.get("task_id")

        try:
            if action == "create_node":
                return await self._create_node(arguments, task_id)
            elif action == "create_edge":
                return await self._create_edge(arguments)
            elif action == "append_decision":
                return await self._append_decision(arguments, task_id)
            elif action == "update_node":
                return await self._update_node(arguments)
            elif action == "delete_node":
                return await self._delete_node(arguments)
            else:
                return ToolResult(output=f"Unknown action: {action}", is_error=True)
        except Exception as e:
            logger.error("memory_write_error", action=action, error=str(e))
            return ToolResult(output=f"Error: {e}", is_error=True)

    async def _create_node(self, args: dict, task_id: int | None) -> ToolResult:
        name = args.get("name")
        node_type = args.get("node_type")
        content = args.get("content", "")
        if not name:
            return ToolResult(output="Error: 'name' is required for create_node", is_error=True)
        if not node_type:
            return ToolResult(output="Error: 'node_type' is required for create_node", is_error=True)

        node = MemoryNode(
            name=name,
            node_type=node_type,
            content=content,
            created_by_task_id=task_id,
        )
        async with async_session() as session:
            session.add(node)
            await session.commit()
            await session.refresh(node)
            return ToolResult(
                output=f"Created node '{name}' (type: {node_type}, id: {node.id})"
            )

    async def _create_edge(self, args: dict) -> ToolResult:
        source_id = args.get("source_id")
        target_id = args.get("target_id")
        relation = args.get("relation")
        if not all([source_id, target_id, relation]):
            return ToolResult(
                output="Error: 'source_id', 'target_id', and 'relation' are required for create_edge",
                is_error=True,
            )
        edge = MemoryEdge(
            source_id=uuid.UUID(source_id),
            target_id=uuid.UUID(target_id),
            relation=relation,
        )
        async with async_session() as session:
            session.add(edge)
            await session.commit()
            return ToolResult(output=f"Created edge: {source_id} --[{relation}]--> {target_id}")

    async def _append_decision(self, args: dict, task_id: int | None) -> ToolResult:
        parent_id = args.get("node_id")
        content = args.get("content", "")
        relation = args.get("relation", "evolved-from")
        if not parent_id:
            return ToolResult(output="Error: 'node_id' is required for append_decision", is_error=True)
        if not content:
            return ToolResult(output="Error: 'content' is required for append_decision", is_error=True)

        async with async_session() as session:
            result = await session.execute(
                select(MemoryNode).where(MemoryNode.id == uuid.UUID(parent_id))
            )
            parent = result.scalar_one_or_none()
            if not parent:
                return ToolResult(output=f"Error: node {parent_id} not found", is_error=True)

            new_node = MemoryNode(
                name=parent.name,
                node_type="decision",
                content=content,
                created_by_task_id=task_id,
            )
            session.add(new_node)
            await session.flush()

            edge = MemoryEdge(
                source_id=new_node.id,
                target_id=uuid.UUID(parent_id),
                relation=relation,
            )
            session.add(edge)
            await session.commit()

            return ToolResult(
                output=f"Appended decision to '{parent.name}' chain (new id: {new_node.id})"
            )

    async def _update_node(self, args: dict) -> ToolResult:
        node_id = args.get("node_id")
        content = args.get("content")
        if not node_id:
            return ToolResult(output="Error: 'node_id' is required for update_node", is_error=True)
        if not content:
            return ToolResult(output="Error: 'content' is required for update_node", is_error=True)

        async with async_session() as session:
            result = await session.execute(
                select(MemoryNode).where(MemoryNode.id == uuid.UUID(node_id))
            )
            node = result.scalar_one_or_none()
            if not node:
                return ToolResult(output=f"Error: node {node_id} not found", is_error=True)
            node.content = content
            await session.commit()
            return ToolResult(output=f"Updated node '{node.name}' content")

    async def _delete_node(self, args: dict) -> ToolResult:
        node_id = args.get("node_id")
        if not node_id:
            return ToolResult(output="Error: 'node_id' is required for delete_node", is_error=True)

        uid = uuid.UUID(node_id)
        async with async_session() as session:
            result = await session.execute(
                select(MemoryNode).where(MemoryNode.id == uid)
            )
            node = result.scalar_one_or_none()
            if not node:
                return ToolResult(output=f"Error: node {node_id} not found", is_error=True)
            name = node.name
            await session.execute(
                delete(MemoryEdge).where(
                    (MemoryEdge.source_id == uid) | (MemoryEdge.target_id == uid)
                )
            )
            await session.delete(node)
            await session.commit()
            return ToolResult(output=f"Deleted node '{name}' and its edges")
