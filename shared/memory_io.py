"""Thin async wrapper around team_memory.graph.GraphEngine.

Keeps web handlers slim and gives us a single seam to mock in tests.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from team_memory.graph import GraphEngine

from shared.database import team_memory_session

if TYPE_CHECKING:
    from shared.types import ProposedFact

logger = structlog.get_logger()


async def recall_entity(name: str) -> dict[str, Any] | None:
    """Return the top match for an entity name, or None.

    Shape: {"entity": {...}, "facts": [...], "score": float}
    """
    if team_memory_session is None:
        return None
    try:
        async with team_memory_session() as session:
            engine = GraphEngine(session)
            result = await engine.recall(query=name)
    except Exception as e:
        logger.warning("memory_recall_failed", name=name, error=str(e))
        return None
    matches = result.get("matches") or []
    if not matches:
        return None
    return matches[0]


async def remember_row(row: ProposedFact, *, author: str | None = None) -> str:
    """Persist a new fact. Returns the new fact_id."""
    async with team_memory_session() as session:
        engine = GraphEngine(session)
        result = await engine.remember(
            content=row.content,
            entity=row.entity,
            entity_type=row.entity_type,
            kind=row.kind,
            source="memory-tab",
            author=author,
        )
    return result.get("fact_id", "")


async def correct_fact(fact_id: str, new_content: str, *, author: str | None = None) -> str:
    """Supersede an existing fact with new content via the correct flow."""
    async with team_memory_session() as session:
        engine = GraphEngine(session)
        result = await engine.correct(
            fact_id=fact_id,
            new_content=new_content,
            reason="updated via memory tab",
            source="memory-tab",
            author=author,
        )
    return result.get("fact_id", "")
