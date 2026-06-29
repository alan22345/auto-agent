"""Backend-agnostic team-memory access for recall / remember / correct.

When ``TEAM_MEMORY_MCP_URL`` + ``TEAM_MEMORY_MCP_TOKEN`` are configured, these
go through the hosted team-memory MCP server over HTTP, so the orchestrator
needs no direct database access (the DB can live on a private network, e.g.
Fly). When they are not set, we fall back to a direct ``GraphEngine`` session
(the legacy path) — so deploying this with no token configured is a no-op.

This is the single seam introduced for the team-memory → Fly migration. Note:
``recall``/``remember``/``correct`` are the only operations the team-memory MCP
exposes. Listing/detail/delete (web Memory Browser) and ``resolve``/``_facts_for``
(repo-map cache) have NO MCP equivalent and still require direct DB access — see
``shared/memory_io.py`` and ``agent/context/system.py``.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from shared.config import settings

logger = structlog.get_logger()


def _http_enabled() -> bool:
    """True when the hosted HTTP MCP backend is configured."""
    url = (getattr(settings, "team_memory_mcp_url", "") or "").strip()
    token = (getattr(settings, "team_memory_mcp_token", "") or "").strip()
    return bool(url and token)


def configured() -> bool:
    """True when team-memory is reachable by either backend (HTTP or direct DB)."""
    if _http_enabled():
        return True
    from shared.database import team_memory_session

    return team_memory_session is not None


async def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a team-memory MCP tool over HTTP and parse its JSON result.

    The team-memory MCP returns tool results as a JSON text block (its dict
    return is not surfaced as structuredContent in the current SDK), so we read
    ``content[0].text`` and ``json.loads`` it.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = settings.team_memory_mcp_url.strip()
    token = settings.team_memory_mcp_token.strip()
    headers = {"Authorization": f"Bearer {token}"}
    args = {k: v for k, v in arguments.items() if v is not None}

    async with streamablehttp_client(url, headers=headers) as (read, write, _), ClientSession(
        read, write
    ) as session:
        await session.initialize()
        result = await session.call_tool(name, args)
        text = ""
        for block in getattr(result, "content", None) or []:
            t = getattr(block, "text", None)
            if t is not None:
                text += t
        if getattr(result, "isError", False):
            raise RuntimeError(f"team-memory MCP tool {name} errored: {text[:300]}")
        return json.loads(text) if text else {}


async def recall(
    query: str,
    *,
    context: str | None = None,
    include_history: bool = False,
    max_results: int = 10,
) -> dict[str, Any]:
    if _http_enabled():
        return await _call_tool(
            "recall",
            {
                "query": query,
                "context": context,
                "include_history": include_history,
                "max_results": max_results,
            },
        )
    # fallback: direct GraphEngine
    from team_memory.graph import GraphEngine

    from shared.database import team_memory_session

    if team_memory_session is None:
        return {"matches": [], "ambiguous": False}
    async with team_memory_session() as session:
        return await GraphEngine(session).recall(
            query=query,
            context=context,
            include_history=include_history,
            max_results=max_results,
        )


async def remember(
    *,
    content: str,
    entity: str,
    entity_type: str,
    kind: str,
    source: str | None = None,
    author: str | None = None,
    tags: list[str] | None = None,
    aliases: list[str] | None = None,
    file_anchors: list[str] | None = None,
) -> dict[str, Any]:
    if _http_enabled():
        return await _call_tool(
            "remember",
            {
                "content": content,
                "entity": entity,
                "entity_type": entity_type,
                "kind": kind,
                "source": source,
                "author": author,
                "tags": tags,
                "aliases": aliases,
                "file_anchors": file_anchors,
            },
        )
    from team_memory.graph import GraphEngine

    from shared.database import team_memory_session

    if team_memory_session is None:
        return {}
    async with team_memory_session() as session:
        result = await GraphEngine(session).remember(
            content=content,
            entity=entity,
            entity_type=entity_type,
            kind=kind,
            source=source,
            author=author,
            tags=tags,
            aliases=aliases,
            file_anchors=file_anchors,
        )
        await session.commit()
        return result


async def correct(
    *,
    fact_id: str,
    new_content: str,
    reason: str,
    source: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    if _http_enabled():
        return await _call_tool(
            "correct",
            {
                "fact_id": fact_id,
                "new_content": new_content,
                "reason": reason,
                "source": source,
                "author": author,
            },
        )
    from team_memory.graph import GraphEngine

    from shared.database import team_memory_session

    if team_memory_session is None:
        return {}
    async with team_memory_session() as session:
        result = await GraphEngine(session).correct(
            fact_id=fact_id,
            new_content=new_content,
            reason=reason,
            source=source,
            author=author,
        )
        await session.commit()
        return result
