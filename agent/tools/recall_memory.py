"""Recall facts from the shared team-memory graph."""

from __future__ import annotations

import contextlib
import json
from typing import Any, ClassVar

import structlog
from team_memory.graph import GraphEngine

from agent.tools.base import Tool, ToolContext, ToolResult
from shared.database import team_memory_session

logger = structlog.get_logger()


class RecallMemoryTool(Tool):
    name = "recall_memory"
    description = (
        "Look up facts in the shared team-memory knowledge graph by entity "
        "name or topic. Use this BEFORE searching the web when the question "
        "is about the team, the project, or anything previously remembered."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Entity name or topic to recall.",
            },
        },
        "required": ["query"],
    }
    is_readonly = True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        query = (arguments.get("query") or "").strip()
        if not query:
            return ToolResult(output="Error: 'query' is required.", is_error=True)

        if team_memory_session is None:
            return ToolResult(
                output="Error: team-memory is not configured on this server.",
                is_error=True,
            )

        try:
            async with team_memory_session() as session:
                engine = GraphEngine(session)
                result = await engine.recall(query=query)
        except Exception as e:
            logger.warning("recall_memory_failed", error=str(e), query=query)
            return ToolResult(output=f"Error recalling memory: {e}", is_error=True)

        if context.event_sink is not None:
            for match in result.get("matches") or []:
                with contextlib.suppress(Exception):
                    await context.event_sink({
                        "type": "memory_hit",
                        "entity": match["entity"],
                        "facts": match["facts"],
                    })

        return ToolResult(output=json.dumps(result, ensure_ascii=False))
