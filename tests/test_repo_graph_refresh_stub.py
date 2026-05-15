"""Refresh endpoint is the **one** boundary stub permitted in Phase 1.

ADR-016 §10 spells out the full analyser pipeline; the refresh endpoint
is the surface that triggers it. Phase 1 ships the surface (so the UI is
demoable) and returns HTTP 501 with a body that names Phase 2 of ADR-016
explicitly — both as a guard against accidental "stubs everywhere" drift
and so a curious operator hitting the endpoint sees what's going on.

If somebody removes the Phase-2 reference from the response body, this
test fails — exactly the deletion-test discipline ADR-015 §8 requires.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

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
async def test_refresh_returns_501_with_phase_2_message(mock_get_repo) -> None:
    from orchestrator.router import refresh_repo_graph

    repo = _make_repo()
    cfg = _make_config()
    mock_get_repo.return_value = repo
    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = cfg
    session.execute.return_value = mock_result

    with pytest.raises(HTTPException) as exc:
        await refresh_repo_graph(repo_id=repo.id, session=session, org_id=1)

    assert exc.value.status_code == 501
    detail = str(exc.value.detail).lower()
    # Must explicitly mention Phase 2 of ADR-016 — see ADR §"Implementation
    # phasing" item 2 and CLAUDE.md no-defer rule.
    assert "phase 2" in detail
    assert "adr-016" in detail


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_refresh_404_when_repo_missing(mock_get_repo) -> None:
    from orchestrator.router import refresh_repo_graph

    mock_get_repo.return_value = None
    session = AsyncMock(spec=AsyncSession)

    with pytest.raises(HTTPException) as exc:
        await refresh_repo_graph(repo_id=99, session=session, org_id=1)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_refresh_404_when_graph_not_enabled(mock_get_repo) -> None:
    from orchestrator.router import refresh_repo_graph

    mock_get_repo.return_value = _make_repo()
    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    with pytest.raises(HTTPException) as exc:
        await refresh_repo_graph(repo_id=1, session=session, org_id=1)
    assert exc.value.status_code == 404
