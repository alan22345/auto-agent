"""Staleness endpoint tests (ADR-016 Phase 7 §11 — freshness banner polish).

``GET /api/repos/{repo_id}/graph/staleness`` lets the freshness banner
poll for "workspace has moved since this graph was generated" drift
without re-fetching the whole graph blob. The endpoint is a thin
wrapper around :func:`agent.graph_analyzer.staleness.compute_staleness`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from agent.graph_analyzer.staleness import Staleness
from shared.models import Repo, RepoGraph, RepoGraphConfig


def _make_repo(*, repo_id: int = 1):
    repo = MagicMock(spec=Repo)
    repo.id = repo_id
    repo.name = "demo"
    repo.organization_id = 1
    return repo


def _make_config(*, repo_id: int = 1, last_analysis_id: int | None = 42):
    cfg = MagicMock(spec=RepoGraphConfig)
    cfg.repo_id = repo_id
    cfg.organization_id = 1
    cfg.analysis_branch = "main"
    cfg.workspace_path = f"/data/graph-workspaces/{repo_id}"
    cfg.last_analysis_id = last_analysis_id
    return cfg


def _make_row(*, sha: str = "deadbeef" * 5):
    row = MagicMock(spec=RepoGraph)
    row.id = 42
    row.commit_sha = sha
    return row


def _session_with_results(*results):
    """Return an AsyncSession that yields ``results`` from successive
    ``session.execute(...)`` calls — each item becomes the
    ``scalar_one_or_none`` of the corresponding execute call."""

    session = AsyncMock(spec=AsyncSession)
    side_effects = []
    for r in results:
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = r
        side_effects.append(mock_result)
    session.execute.side_effect = side_effects
    return session


class TestGraphStalenessEndpoint:
    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_repo_not_found_returns_404(self, mock_get_repo) -> None:
        from orchestrator.router import get_repo_graph_staleness

        mock_get_repo.return_value = None
        session = AsyncMock(spec=AsyncSession)

        with pytest.raises(HTTPException) as exc:
            await get_repo_graph_staleness(
                repo_id=99,
                session=session,
                org_id=1,
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_graph_not_enabled_returns_404(self, mock_get_repo) -> None:
        from orchestrator.router import get_repo_graph_staleness

        mock_get_repo.return_value = _make_repo()
        session = _session_with_results(None)  # config lookup returns None

        with pytest.raises(HTTPException) as exc:
            await get_repo_graph_staleness(
                repo_id=1,
                session=session,
                org_id=1,
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_no_analysis_yet_returns_404(self, mock_get_repo) -> None:
        # Config exists but no analysis has completed yet — there is no
        # graph_sha to compare against.
        from orchestrator.router import get_repo_graph_staleness

        repo = _make_repo()
        cfg = _make_config(repo_id=repo.id, last_analysis_id=None)
        mock_get_repo.return_value = repo
        session = _session_with_results(cfg)

        with pytest.raises(HTTPException) as exc:
            await get_repo_graph_staleness(
                repo_id=repo.id,
                session=session,
                org_id=1,
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @patch("orchestrator.router.compute_staleness")
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_returns_drifted_envelope_when_workspace_head_differs(
        self,
        mock_get_repo,
        mock_compute,
    ) -> None:
        from orchestrator.router import get_repo_graph_staleness

        repo = _make_repo()
        cfg = _make_config(repo_id=repo.id, last_analysis_id=42)
        row = _make_row(sha="aaaaaaa" + "a" * 33)
        mock_get_repo.return_value = repo
        session = _session_with_results(cfg, row)
        mock_compute.return_value = Staleness(
            graph_sha=row.commit_sha,
            workspace_sha="bbbbbbb" + "b" * 33,
            drifted=True,
        )

        out = await get_repo_graph_staleness(
            repo_id=repo.id,
            session=session,
            org_id=1,
        )

        # The endpoint must have called compute_staleness with the
        # row's SHA + the config's workspace path.
        mock_compute.assert_called_once_with(
            graph_sha=row.commit_sha,
            workspace_path=cfg.workspace_path,
        )
        assert out.graph_sha == row.commit_sha
        assert out.workspace_sha == "bbbbbbb" + "b" * 33
        assert out.drifted is True

    @pytest.mark.asyncio
    @patch("orchestrator.router.compute_staleness")
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_returns_not_drifted_when_workspace_matches_graph_sha(
        self,
        mock_get_repo,
        mock_compute,
    ) -> None:
        from orchestrator.router import get_repo_graph_staleness

        repo = _make_repo()
        cfg = _make_config(repo_id=repo.id, last_analysis_id=42)
        sha = "c" * 40
        row = _make_row(sha=sha)
        mock_get_repo.return_value = repo
        session = _session_with_results(cfg, row)
        mock_compute.return_value = Staleness(
            graph_sha=sha,
            workspace_sha=sha,
            drifted=False,
        )

        out = await get_repo_graph_staleness(
            repo_id=repo.id,
            session=session,
            org_id=1,
        )
        assert out.graph_sha == sha
        assert out.workspace_sha == sha
        assert out.drifted is False

    @pytest.mark.asyncio
    @patch("orchestrator.router.compute_staleness")
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_missing_workspace_returns_drifted_with_null_workspace_sha(
        self,
        mock_get_repo,
        mock_compute,
    ) -> None:
        # When the analyser workspace is gone (e.g. someone deleted the
        # directory between analyses) the primitive returns
        # ``workspace_sha=None, drifted=True`` and the endpoint must
        # surface that honestly rather than 500.
        from orchestrator.router import get_repo_graph_staleness

        repo = _make_repo()
        cfg = _make_config(repo_id=repo.id, last_analysis_id=42)
        sha = "d" * 40
        row = _make_row(sha=sha)
        mock_get_repo.return_value = repo
        session = _session_with_results(cfg, row)
        mock_compute.return_value = Staleness(
            graph_sha=sha,
            workspace_sha=None,
            drifted=True,
        )

        out = await get_repo_graph_staleness(
            repo_id=repo.id,
            session=session,
            org_id=1,
        )
        assert out.graph_sha == sha
        assert out.workspace_sha is None
        assert out.drifted is True

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_404_when_analysis_id_points_to_missing_row(
        self,
        mock_get_repo,
    ) -> None:
        # Defensive: the config can reference a RepoGraph id that no
        # longer exists (manual DB surgery, race during disable). The
        # endpoint returns 404 rather than crashing on a NoneType row.
        from orchestrator.router import get_repo_graph_staleness

        repo = _make_repo()
        cfg = _make_config(repo_id=repo.id, last_analysis_id=42)
        mock_get_repo.return_value = repo
        session = _session_with_results(cfg, None)  # row lookup returns None

        with pytest.raises(HTTPException) as exc:
            await get_repo_graph_staleness(
                repo_id=repo.id,
                session=session,
                org_id=1,
            )
        assert exc.value.status_code == 404
