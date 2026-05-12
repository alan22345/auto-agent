"""Tests for GET /api/repos/{repo_id}/market-brief/latest.

Uses FastAPI dependency_overrides + httpx.AsyncClient (the same pattern as
test_search_endpoint.py): no real DB required, the session is an AsyncMock
that returns canned MarketBrief objects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from orchestrator.auth import create_token
from orchestrator.auth import current_org_id as current_org_id_dep
from orchestrator.router import router
from shared.database import get_session


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


def _bearer(user_id: int = 1, username: str = "alice", org_id: int = 1) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_token(user_id, username, current_org_id=org_id)}"}


def _make_brief(
    brief_id: int = 1,
    repo_id: int = 10,
    org_id: int = 1,
    created_at: datetime | None = None,
) -> MagicMock:
    """Build a fake MarketBrief using MagicMock (avoids SQLAlchemy instrumentation)."""
    brief = MagicMock()
    brief.id = brief_id
    brief.repo_id = repo_id
    brief.organization_id = org_id
    brief.created_at = created_at or datetime.now(UTC)
    brief.product_category = "Developer tools"
    brief.competitors = [{"name": "CompetitorX"}]
    brief.findings = [{"title": "Finding 1", "body": "..."}]
    brief.modality_gaps = []
    brief.strategic_themes = [{"theme": "AI integration"}]
    brief.summary = "A concise market summary."
    brief.partial = False
    return brief


# ---------------------------------------------------------------------------
# Test 1: 404 when no brief exists for the repo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_market_brief_endpoint_returns_404_when_none():
    """Returns HTTP 404 when no MarketBrief exists for the requested repo."""
    session = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=execute_result)

    async def _override_session():
        yield session

    app = _make_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[current_org_id_dep] = lambda: 1
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get(
                "/api/repos/10/market-brief/latest",
                headers=_bearer(org_id=1),
            )
        assert resp.status_code == 404, resp.text
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Test 2: Returns the latest (most recently created) brief when multiple exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_market_brief_endpoint_returns_latest():
    """Returns the most recent brief; verifies response shape."""
    now = datetime.now(UTC)
    fresh_brief = _make_brief(brief_id=2, repo_id=10, org_id=1, created_at=now)

    session = AsyncMock()
    execute_result = MagicMock()
    # The endpoint queries with .limit(1) ordered by created_at desc, so only
    # the latest brief is returned by the DB.  We simulate that here.
    execute_result.scalar_one_or_none.return_value = fresh_brief
    session.execute = AsyncMock(return_value=execute_result)

    async def _override_session():
        yield session

    app = _make_app()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[current_org_id_dep] = lambda: 1
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            resp = await c.get(
                "/api/repos/10/market-brief/latest",
                headers=_bearer(org_id=1),
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == 2
        assert body["repo_id"] == 10
        assert body["partial"] is False
        assert body["summary"] == "A concise market summary."
        assert body["product_category"] == "Developer tools"
        assert isinstance(body["competitors"], list)
        assert isinstance(body["findings"], list)
        assert isinstance(body["modality_gaps"], list)
        assert isinstance(body["strategic_themes"], list)
        assert "created_at" in body
        # created_at must be an ISO 8601 string
        datetime.fromisoformat(body["created_at"])
    finally:
        app.dependency_overrides.clear()
