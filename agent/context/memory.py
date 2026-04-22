"""Graph memory context — queries relevant memory for task injection."""

from __future__ import annotations

import structlog

from shared.database import async_session
from team_memory.graph import GraphEngine

logger = structlog.get_logger()


async def query_relevant_memory(task_description: str) -> str:
    """Query graph memory for context relevant to a task description.

    Returns formatted context string or empty string if nothing found.
    """
    if not task_description:
        return ""

    try:
        async with async_session() as session:
            engine = GraphEngine(session)
            result = await engine.recall(query=task_description)

        if result.get("ambiguous") or not result.get("matches"):
            return ""

        parts = ["## Shared Team Memory (relevant to this task)\n"]
        for match in result["matches"]:
            entity = match["entity"]
            facts = match["facts"]
            header = f"- **[{entity['type']}] {entity['name']}**"
            parts.append(header)
            for fact in facts:
                source_note = f" (source: {fact['source']})" if fact.get("source") else ""
                parts.append(f"  - [{fact['kind']}] {fact['content']}{source_note}")

        return "\n".join(parts)

    except Exception as e:
        logger.warning("memory_recall_failed", error=str(e))
        return ""
