"""Graph memory context — queries relevant memory for task injection."""

from __future__ import annotations

import structlog
from team_memory.graph import GraphEngine

from shared.database import team_memory_session

logger = structlog.get_logger()


# Suggestion priorities that warrant a permanent team-memory entry.
# Suggestion.priority is 1=critical, 5=nice-to-have (shared/models.py:184),
# so 1-2 = "high urgency".
_HIGH_PRIORITY_THRESHOLD = 2


async def remember_priority_suggestion(
    *,
    repo_name: str,
    title: str,
    rationale: str,
    priority: int,
    category: str,
    source: str,
) -> None:
    """Persist a high-priority analyzer suggestion to the shared graph.

    Skips silently if priority is below the threshold, team-memory isn't
    configured, or the write fails — the suggestion is already in the
    suggestions table either way, so memory persistence is best-effort.
    """
    if priority is None or priority > _HIGH_PRIORITY_THRESHOLD:
        return
    if team_memory_session is None:
        return

    fact = (
        f"[{category}, P{priority}] {title}. Why: "
        f"{(rationale or '').strip()[:600]}"
    )
    try:
        async with team_memory_session() as session:
            engine = GraphEngine(session)
            await engine.remember(
                content=fact,
                entity=repo_name,
                entity_type="project",
                kind="note",
                source=source,
                author=None,
            )
            await session.commit()
    except Exception as e:
        logger.warning(
            "remember_priority_suggestion_failed",
            error=str(e),
            repo=repo_name,
            title=title,
        )


async def query_relevant_memory(task_description: str) -> str:
    """Query graph memory for context relevant to a task description.

    Returns formatted context string or empty string if nothing found.
    """
    if not task_description:
        return ""

    if team_memory_session is None:
        return ""

    try:
        async with team_memory_session() as session:
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
