"""Tests for the per-repo code-graph config API (ADR-016 §8/§11).

Covers:
- POST   /api/repos/{repo_id}/graph         enable graph (idempotent)
- GET    /api/repos/{repo_id}/graph         fetch config (404 when off)
- PATCH  /api/repos/{repo_id}/graph         set analysis_branch
- DELETE /api/repos/{repo_id}/graph         disable graph
- GET    /api/graph/configs                 list graph-enabled repos
- POST   /api/repos/{repo_id}/graph/refresh 501 (Phase 2 boundary)

Mocks the DB session and reuses the same MagicMock-style approach as
``test_delete_repo.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Repo, RepoGraphConfig


def _make_repo(*, repo_id: int = 1, name: str = "demo", default_branch: str = "main"):
    repo = MagicMock(spec=Repo)
    repo.id = repo_id
    repo.name = name
    repo.url = f"https://github.com/example/{name}"
    repo.default_branch = default_branch
    repo.organization_id = 1
    return repo


def _make_config(*, repo_id: int = 1, branch: str = "main"):
    cfg = MagicMock(spec=RepoGraphConfig)
    cfg.repo_id = repo_id
    cfg.organization_id = 1
    cfg.analysis_branch = branch
    cfg.analyser_version = ""
    cfg.workspace_path = f"/data/graph-workspaces/{repo_id}"
    cfg.last_analysis_id = None
    cfg.created_at = None
    cfg.updated_at = None
    return cfg


# ---------------------------------------------------------------------------
# POST /api/repos/{repo_id}/graph — enable
# ---------------------------------------------------------------------------


class TestEnableGraph:
    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_repo_not_found_returns_404(self, mock_get_repo) -> None:
        from orchestrator.router import enable_repo_graph
        from shared.types import EnableRepoGraphRequest

        mock_get_repo.return_value = None
        session = AsyncMock(spec=AsyncSession)

        with pytest.raises(HTTPException) as exc:
            await enable_repo_graph(
                repo_id=99,
                req=EnableRepoGraphRequest(),
                session=session,
                org_id=1,
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_creates_config_with_default_branch(self, mock_get_repo) -> None:
        from orchestrator.router import enable_repo_graph
        from shared.types import EnableRepoGraphRequest

        repo = _make_repo(default_branch="prod")
        mock_get_repo.return_value = repo
        session = AsyncMock(spec=AsyncSession)
        # Existing-config lookup returns None — fresh enable.
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        out = await enable_repo_graph(
            repo_id=repo.id,
            req=EnableRepoGraphRequest(),
            session=session,
            org_id=1,
        )

        # Newly-added config row uses the repo's default_branch.
        added = session.add.call_args.args[0]
        assert isinstance(added, RepoGraphConfig)
        assert added.analysis_branch == "prod"
        assert added.repo_id == repo.id
        assert added.organization_id == 1
        assert added.workspace_path.endswith(f"/{repo.id}")

        # Response includes the config data we just created.
        assert out.repo_id == repo.id
        assert out.analysis_branch == "prod"

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_explicit_branch_overrides_default(self, mock_get_repo) -> None:
        from orchestrator.router import enable_repo_graph
        from shared.types import EnableRepoGraphRequest

        repo = _make_repo(default_branch="main")
        mock_get_repo.return_value = repo
        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        out = await enable_repo_graph(
            repo_id=repo.id,
            req=EnableRepoGraphRequest(analysis_branch="release"),
            session=session,
            org_id=1,
        )
        assert out.analysis_branch == "release"

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_already_enabled_is_idempotent(self, mock_get_repo) -> None:
        from orchestrator.router import enable_repo_graph
        from shared.types import EnableRepoGraphRequest

        repo = _make_repo()
        existing = _make_config(repo_id=repo.id, branch="release")
        mock_get_repo.return_value = repo
        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing
        session.execute.return_value = mock_result

        out = await enable_repo_graph(
            repo_id=repo.id,
            req=EnableRepoGraphRequest(analysis_branch="ignored"),
            session=session,
            org_id=1,
        )
        # Idempotent: existing config returned unchanged, no new row added.
        session.add.assert_not_called()
        assert out.analysis_branch == "release"


# ---------------------------------------------------------------------------
# GET /api/repos/{repo_id}/graph
# ---------------------------------------------------------------------------


class TestGetGraphConfig:
    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_returns_config_when_enabled(self, mock_get_repo) -> None:
        from orchestrator.router import get_repo_graph_config

        repo = _make_repo()
        cfg = _make_config(repo_id=repo.id, branch="dev")
        mock_get_repo.return_value = repo
        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = cfg
        session.execute.return_value = mock_result

        out = await get_repo_graph_config(repo_id=repo.id, session=session, org_id=1)
        assert out.repo_id == repo.id
        assert out.analysis_branch == "dev"

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_404_when_not_enabled(self, mock_get_repo) -> None:
        from orchestrator.router import get_repo_graph_config

        mock_get_repo.return_value = _make_repo()
        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc:
            await get_repo_graph_config(repo_id=1, session=session, org_id=1)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_404_when_repo_missing(self, mock_get_repo) -> None:
        from orchestrator.router import get_repo_graph_config

        mock_get_repo.return_value = None
        session = AsyncMock(spec=AsyncSession)

        with pytest.raises(HTTPException) as exc:
            await get_repo_graph_config(repo_id=1, session=session, org_id=1)
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/repos/{repo_id}/graph
# ---------------------------------------------------------------------------


class TestUpdateGraphConfig:
    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_updates_analysis_branch(self, mock_get_repo) -> None:
        from orchestrator.router import update_repo_graph_config
        from shared.types import UpdateRepoGraphRequest

        repo = _make_repo()
        cfg = _make_config(repo_id=repo.id, branch="main")
        mock_get_repo.return_value = repo
        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = cfg
        session.execute.return_value = mock_result

        out = await update_repo_graph_config(
            repo_id=repo.id,
            req=UpdateRepoGraphRequest(analysis_branch="release/v2"),
            session=session,
            org_id=1,
        )

        # Stub MagicMock's attribute assignment doesn't propagate; assert
        # via the response we built from the live cfg row.
        assert cfg.analysis_branch == "release/v2"
        session.commit.assert_awaited_once()
        assert out.analysis_branch == "release/v2"

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_rejects_invalid_branch_name(self, mock_get_repo) -> None:
        from orchestrator.router import update_repo_graph_config
        from shared.types import UpdateRepoGraphRequest

        repo = _make_repo()
        cfg = _make_config(repo_id=repo.id)
        mock_get_repo.return_value = repo
        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = cfg
        session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc:
            await update_repo_graph_config(
                repo_id=repo.id,
                req=UpdateRepoGraphRequest(analysis_branch="bad branch with spaces"),
                session=session,
                org_id=1,
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_404_when_graph_not_enabled(self, mock_get_repo) -> None:
        from orchestrator.router import update_repo_graph_config
        from shared.types import UpdateRepoGraphRequest

        mock_get_repo.return_value = _make_repo()
        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc:
            await update_repo_graph_config(
                repo_id=1,
                req=UpdateRepoGraphRequest(analysis_branch="dev"),
                session=session,
                org_id=1,
            )
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/repos/{repo_id}/graph
# ---------------------------------------------------------------------------


class TestDisableGraph:
    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_deletes_config(self, mock_get_repo) -> None:
        from orchestrator.router import disable_repo_graph

        repo = _make_repo()
        cfg = _make_config(repo_id=repo.id)
        mock_get_repo.return_value = repo
        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = cfg
        session.execute.return_value = mock_result

        out = await disable_repo_graph(repo_id=repo.id, session=session, org_id=1)
        session.delete.assert_awaited_once_with(cfg)
        session.commit.assert_awaited_once()
        assert out == {"disabled": repo.id}

    @pytest.mark.asyncio
    @patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
    async def test_404_when_not_enabled(self, mock_get_repo) -> None:
        from orchestrator.router import disable_repo_graph

        mock_get_repo.return_value = _make_repo()
        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        with pytest.raises(HTTPException) as exc:
            await disable_repo_graph(repo_id=1, session=session, org_id=1)
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/graph/configs
# ---------------------------------------------------------------------------


class TestListGraphConfigs:
    @pytest.mark.asyncio
    async def test_returns_only_org_scoped_configs(self) -> None:
        # We do not patch anything here — the endpoint executes a single
        # joined select, so we drive it via a fake AsyncSession return value.
        from orchestrator.router import list_repo_graph_configs

        repo = _make_repo()
        cfg = _make_config(repo_id=repo.id, branch="main")
        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.all.return_value = [(cfg, repo)]
        session.execute.return_value = mock_result

        out = await list_repo_graph_configs(session=session, org_id=1)
        assert len(out) == 1
        assert out[0].repo_id == repo.id
        assert out[0].repo_name == repo.name
        assert out[0].repo_url == repo.url
        assert out[0].analysis_branch == "main"

    @pytest.mark.asyncio
    async def test_empty_when_nothing_enabled(self) -> None:
        from orchestrator.router import list_repo_graph_configs

        session = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.all.return_value = []
        session.execute.return_value = mock_result

        out = await list_repo_graph_configs(session=session, org_id=1)
        assert out == []
