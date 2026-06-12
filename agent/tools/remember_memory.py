"""Persist a fact to the shared team-memory graph.

Tightly scoped: the agent should ONLY call this for user-stated preferences
or durable personal/project facts the user has explicitly asked to remember.
Web research findings are explicitly out of scope.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from agent.tools.base import Tool, ToolContext, ToolResult
from shared import memory_client

logger = structlog.get_logger()


class RememberMemoryTool(Tool):
    name = "remember_memory"
    description = (
        "Save a fact to the shared team-memory graph. Use ONLY when:\n"
        "  (a) the user has explicitly asked you to remember something, OR\n"
        "  (b) the user has stated a durable preference or fact about "
        "themselves or the project that will be useful in future "
        "conversations.\n"
        "DO NOT save research findings, web search summaries, or anything "
        "you learned from web_search / fetch_url. Those belong in the "
        "current search session, not in team-memory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "entity_name": {
                "type": "string",
                "description": "Canonical name of the entity the fact is about (e.g. 'Alan', 'Auto-Agent').",
            },
            "entity_type": {
                "type": "string",
                "description": "Entity type, e.g. 'person', 'project', 'team', 'system'.",
            },
            "fact": {
                "type": "string",
                "description": "The fact to remember, as a single concise sentence.",
            },
            "kind": {
                "type": "string",
                "description": "One of: preference, decision, status, note.",
                "enum": ["preference", "decision", "status", "note"],
            },
        },
        "required": ["entity_name", "entity_type", "fact", "kind"],
    }
    is_readonly = False

    def __init__(self, author: str | None = None) -> None:
        self._author = author

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        for field in ("entity_name", "entity_type", "fact", "kind"):
            if not arguments.get(field):
                return ToolResult(
                    output=f"Error: '{field}' is required.",
                    is_error=True,
                )

        if not memory_client.configured():
            return ToolResult(
                output="Error: team-memory is not configured on this server.",
                is_error=True,
            )

        try:
            result = await memory_client.remember(
                content=arguments["fact"],
                entity=arguments["entity_name"],
                entity_type=arguments["entity_type"],
                kind=arguments["kind"],
                source="search-tab",
                author=self._author,
            )
        except Exception as e:
            logger.warning("remember_memory_failed", error=str(e))
            return ToolResult(output=f"Error remembering fact: {e}", is_error=True)

        return ToolResult(output=json.dumps({"ok": True, **result}, ensure_ascii=False))
