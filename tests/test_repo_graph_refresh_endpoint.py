"""Refresh endpoint tests (ADR-016 Phase 2).

Replaces ``test_repo_graph_refresh_stub.py``. The endpoint is now an
event publisher: returns 202 with a ``request_id``; the actual analysis
runs in the agent process (see ``agent/lifecycle/graph_refresh.py``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from shared.events import InMemoryPublisher, RepoEventType
from shared.models import Repo, RepoGraphConfig


def _make_repo(*, repo_id: int = 1):
    repo = MagicMock(spec=Repo)
    repo.id = repo_id
    repo.name = "demo"
    repo.organization_id = 1
    return repo


def _make_config(*, repo_id: int = 1):
    cfg = MagicMock(spec=RepoGraphConfig)
    cfg.repo_id = repo_id
    cfg.organization_id = 1
    cfg.analysis_branch = "main"
    cfg.workspace_path = f"/data/graph-workspaces/{repo_id}"
    return cfg


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_refresh_publishes_event_and_returns_202(
    mock_get_repo,
    publisher: InMemoryPublisher,
) -> None:
    from orchestrator.router import refresh_repo_graph

    repo = _make_repo()
    cfg = _make_config()
    mock_get_repo.return_value = repo
    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = cfg
    session.execute.return_value = mock_result

    resp = Response()
    out = await refresh_repo_graph(
        repo_id=repo.id,
        response=resp,
        session=session,
        org_id=1,
    )

    assert out.status == "accepted"
    assert out.request_id  # uuid string
    assert resp.status_code == 202

    # Event published exactly once, with the right payload.
    events = [e for e in publisher.events if e.type == RepoEventType.GRAPH_REQUESTED]
    assert len(events) == 1
    ev = events[0]
    assert ev.payload["repo_id"] == repo.id
    assert ev.payload["request_id"] == out.request_id


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_refresh_returns_404_when_repo_missing(
    mock_get_repo,
    publisher: InMemoryPublisher,
) -> None:
    from orchestrator.router import refresh_repo_graph

    mock_get_repo.return_value = None
    session = AsyncMock(spec=AsyncSession)
    resp = Response()
    with pytest.raises(HTTPException) as exc:
        await refresh_repo_graph(repo_id=99, response=resp, session=session, org_id=1)
    assert exc.value.status_code == 404
    # No event published on the 404 path.
    assert not any(e.type == RepoEventType.GRAPH_REQUESTED for e in publisher.events)


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_refresh_returns_404_when_graph_not_enabled(
    mock_get_repo,
    publisher: InMemoryPublisher,
) -> None:
    from orchestrator.router import refresh_repo_graph

    mock_get_repo.return_value = _make_repo()
    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    resp = Response()
    with pytest.raises(HTTPException) as exc:
        await refresh_repo_graph(repo_id=1, response=resp, session=session, org_id=1)
    assert exc.value.status_code == 404
    assert not any(e.type == RepoEventType.GRAPH_REQUESTED for e in publisher.events)


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_refresh_with_area_query_param_propagates_to_event(
    mock_get_repo,
    publisher: InMemoryPublisher,
) -> None:
    """ADR-016 §10 Phase 7 — the optional ``area`` query parameter is
    forwarded onto the published event as ``area_scope`` so the
    analyser handler can dispatch to the partial pipeline."""
    from orchestrator.router import refresh_repo_graph

    repo = _make_repo()
    cfg = _make_config()
    mock_get_repo.return_value = repo
    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = cfg
    session.execute.return_value = mock_result

    resp = Response()
    out = await refresh_repo_graph(
        repo_id=repo.id,
        response=resp,
        session=session,
        org_id=1,
        area="agent",
    )

    assert out.status == "accepted"
    events = [e for e in publisher.events if e.type == RepoEventType.GRAPH_REQUESTED]
    assert len(events) == 1
    assert events[0].payload["area_scope"] == "agent"


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_refresh_without_area_propagates_null_area_scope(
    mock_get_repo,
    publisher: InMemoryPublisher,
) -> None:
    """When the ``area`` query parameter is omitted the published event
    carries ``area_scope=None`` so the handler dispatches to the full
    pipeline (the existing Phase 2 behaviour)."""
    from orchestrator.router import refresh_repo_graph

    repo = _make_repo()
    cfg = _make_config()
    mock_get_repo.return_value = repo
    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = cfg
    session.execute.return_value = mock_result

    resp = Response()
    await refresh_repo_graph(
        repo_id=repo.id,
        response=resp,
        session=session,
        org_id=1,
    )

    events = [e for e in publisher.events if e.type == RepoEventType.GRAPH_REQUESTED]
    assert len(events) == 1
    assert events[0].payload["area_scope"] is None


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_refresh_rejects_invalid_area_name(
    mock_get_repo,
    publisher: InMemoryPublisher,
) -> None:
    """The area name is validated against the same character set the
    branch name uses (alphanumeric, ``.``, ``_``, ``/``, ``-``) to keep
    shell-injection-style risks out of downstream code paths that might
    join the name into a path."""
    from orchestrator.router import refresh_repo_graph

    repo = _make_repo()
    cfg = _make_config()
    mock_get_repo.return_value = repo
    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = cfg
    session.execute.return_value = mock_result

    resp = Response()
    with pytest.raises(HTTPException) as exc:
        await refresh_repo_graph(
            repo_id=repo.id,
            response=resp,
            session=session,
            org_id=1,
            area="../../etc",
        )
    assert exc.value.status_code == 400
    assert not any(
        e.type == RepoEventType.GRAPH_REQUESTED for e in publisher.events
    )


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_concurrent_refresh_publishes_separate_request_ids(
    mock_get_repo,
    publisher: InMemoryPublisher,
) -> None:
    """Duplicate-refresh handling: the endpoint is fire-and-forget; the
    handler detects lock contention and publishes ``REPO_GRAPH_FAILED``
    with ``"analysis already running"``. Two POSTs therefore produce two
    REQUESTED events with distinct request_ids — the second handler's
    FAILED event is what surfaces the contention to the user."""
    from orchestrator.router import refresh_repo_graph

    repo = _make_repo()
    cfg = _make_config()
    mock_get_repo.return_value = repo
    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = cfg
    session.execute.return_value = mock_result

    r1 = await refresh_repo_graph(
        repo_id=repo.id,
        response=Response(),
        session=session,
        org_id=1,
    )
    r2 = await refresh_repo_graph(
        repo_id=repo.id,
        response=Response(),
        session=session,
        org_id=1,
    )
    assert r1.request_id != r2.request_id
    events = [e for e in publisher.events if e.type == RepoEventType.GRAPH_REQUESTED]
    assert len(events) == 2
