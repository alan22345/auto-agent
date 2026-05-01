"""Thin async wrapper around team_memory.graph.GraphEngine.

Keeps web handlers slim and gives us a single seam to mock in tests.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, select
from team_memory.graph import GraphEngine
from team_memory.models import Entity, Fact

from shared.database import team_memory_session

if TYPE_CHECKING:
    from shared.types import (
        MemoryEntityDetail,
        MemoryEntitySummary,
        MemoryFact,
        ProposedFact,
    )

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


async def correct_fact(
    fact_id: str,
    new_content: str,
    *,
    reason: str | None = None,
    author: str | None = None,
) -> str:
    """Supersede an existing fact with new content via the correct flow.

    `reason` is the user-supplied explanation; falls back to a generic marker
    when omitted so existing call sites (memory_save replace flow) keep working.
    """
    async with team_memory_session() as session:
        engine = GraphEngine(session)
        result = await engine.correct(
            fact_id=fact_id,
            new_content=new_content,
            reason=reason or "updated via memory tab",
            source="memory-tab",
            author=author,
        )
    return result.get("fact_id", "")


def _summary_from_match(match: dict[str, Any]) -> MemoryEntitySummary:
    from shared.types import MemoryEntitySummary

    ent = match.get("entity") or {}
    facts = match.get("facts") or []
    latest = None
    for f in facts:
        vf = f.get("valid_from")
        if vf and (latest is None or vf > latest):
            latest = vf
    return MemoryEntitySummary(
        id=str(ent.get("id", "")),
        name=ent.get("name", ""),
        type=ent.get("type", ""),
        tags=list(ent.get("tags") or []),
        fact_count=sum(1 for f in facts if not f.get("valid_until")),
        latest_fact_at=latest,
    )


async def search_entities(query: str, limit: int = 20) -> list[MemoryEntitySummary]:
    """Search entities by name/alias/tag/fuzzy.

    Empty/whitespace query returns []; the recent-list endpoint should be used
    for the default view.
    """
    q = (query or "").strip()
    if not q:
        return []
    if team_memory_session is None:
        return []
    try:
        async with team_memory_session() as session:
            engine = GraphEngine(session)
            result = await engine.recall(query=q, max_results=limit)
    except Exception as e:
        logger.warning("memory_search_failed", query=q, error=str(e))
        return []
    return [_summary_from_match(m) for m in (result.get("matches") or [])]


async def list_recent_entities(limit: int = 20) -> list[MemoryEntitySummary]:
    """Return the entities with the most recent current-fact activity.

    GraphEngine has no public listing primitive, so we query the entity/fact
    tables directly through the same async session. Only non-archived entities
    with at least one current (valid_until IS NULL) fact are included.
    """
    from shared.types import MemoryEntitySummary

    if team_memory_session is None:
        return []
    try:
        async with team_memory_session() as session:
            latest_at = func.max(Fact.valid_from).label("latest_at")
            fact_count = func.count(Fact.id).label("fact_count")
            stmt = (
                select(
                    Entity.id,
                    Entity.name,
                    Entity.entity_type,
                    Entity.tags,
                    fact_count,
                    latest_at,
                )
                .join(Fact, Fact.entity_id == Entity.id)
                .where(Entity.archived.is_(False), Fact.valid_until.is_(None))
                .group_by(Entity.id, Entity.name, Entity.entity_type, Entity.tags)
                .order_by(latest_at.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()
    except Exception as e:
        logger.warning("memory_list_recent_failed", error=str(e))
        return []
    return [
        MemoryEntitySummary(
            id=str(r.id),
            name=r.name,
            type=r.entity_type,
            tags=list(r.tags or []),
            fact_count=int(r.fact_count or 0),
            latest_fact_at=r.latest_at.isoformat() if r.latest_at else None,
        )
        for r in rows
    ]


def _fact_from_row(f: Fact) -> MemoryFact:
    from shared.types import MemoryFact

    return MemoryFact(
        id=str(f.id),
        content=f.content,
        kind=f.kind,
        source=f.source,
        author=f.author,
        valid_from=f.valid_from.isoformat() if f.valid_from else None,
        valid_until=f.valid_until.isoformat() if f.valid_until else None,
    )


async def get_entity_with_facts(
    name_or_id: str, *, include_superseded: bool = False
) -> MemoryEntityDetail | None:
    """Fetch one entity and its facts. Default hides superseded (valid_until set)."""
    from shared.types import MemoryEntityDetail, MemoryEntitySummary

    if team_memory_session is None:
        return None
    try:
        async with team_memory_session() as session:
            ent = await _resolve_entity(session, name_or_id)
            if ent is None:
                return None
            stmt = select(Fact).where(Fact.entity_id == ent.id)
            if not include_superseded:
                stmt = stmt.where(Fact.valid_until.is_(None))
            stmt = stmt.order_by(Fact.valid_from.desc())
            facts = list((await session.execute(stmt)).scalars())
    except Exception as e:
        logger.warning("memory_get_entity_failed", name_or_id=name_or_id, error=str(e))
        return None

    current_count = sum(1 for f in facts if f.valid_until is None)
    latest = max((f.valid_from for f in facts if f.valid_from), default=None)
    summary = MemoryEntitySummary(
        id=str(ent.id),
        name=ent.name,
        type=ent.entity_type,
        tags=list(ent.tags or []),
        fact_count=current_count,
        latest_fact_at=latest.isoformat() if latest else None,
    )
    return MemoryEntityDetail(entity=summary, facts=[_fact_from_row(f) for f in facts])


async def _resolve_entity(session, name_or_id: str) -> Entity | None:
    """Look up an entity by UUID or case-insensitive name."""
    try:
        uid = uuid.UUID(name_or_id)
        ent = (await session.execute(select(Entity).where(Entity.id == uid))).scalar_one_or_none()
        if ent:
            return ent
    except ValueError:
        pass
    return (
        await session.execute(select(Entity).where(func.lower(Entity.name) == name_or_id.lower()))
    ).scalar_one_or_none()


async def delete_fact(fact_id: str, *, author: str | None = None) -> bool:
    """Soft-delete a fact by setting valid_until = now() with no replacement.

    GraphEngine exposes `correct` (which creates a successor) but no plain
    delete, so we set valid_until directly. The fact stays in the table for
    audit; it just stops being current. Returns True on success, False if the
    fact_id is unknown.
    """
    if team_memory_session is None:
        return False
    try:
        uid = uuid.UUID(fact_id)
    except ValueError:
        return False
    async with team_memory_session() as session:
        fact = (await session.execute(select(Fact).where(Fact.id == uid))).scalar_one_or_none()
        if fact is None:
            return False
        fact.valid_until = func.now()
        if author and not fact.source:
            fact.source = f"deleted via memory tab by {author}"
        await session.commit()
    return True
