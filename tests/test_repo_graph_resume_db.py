"""DB-backed integration: row-load cases in run_refresh."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from shared.database import async_session
from shared.models import Repo, RepoGraph, RepoGraphConfig


def _skip_if_no_db():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("requires DATABASE_URL")


@pytest.mark.asyncio
async def test_noop_on_unchanged_commit(monkeypatch, tmp_path):
    _skip_if_no_db()
    from agent.lifecycle import graph_refresh

    async with async_session() as s:
        repo = Repo(name="cardamon-noop", url="...", default_branch="main",
                    organization_id=1)
        s.add(repo)
        await s.flush()
        s.add(RepoGraphConfig(
            repo_id=repo.id, organization_id=1, analysis_branch="main",
            analyser_version="", workspace_path=str(tmp_path / "ws"),
        ))
        s.add(RepoGraph(
            repo_id=repo.id, commit_sha="AAA", status="ok",
            analyser_version="v1", graph_json={"nodes": [], "edges": []},
            is_complete=True, processed_files={"x.py": {}}, failed_sites=[],
        ))
        await s.commit()
        repo_id = repo.id

    monkeypatch.setattr(graph_refresh, "_prepare_workspace", AsyncMock())
    monkeypatch.setattr(graph_refresh, "_resolve_commit_sha", AsyncMock(return_value="AAA"))
    monkeypatch.setattr(
        "agent.graph_workspace.graph_workspace_path",
        lambda repo_id: str(tmp_path / "ws"),
    )
    pipeline_spy = AsyncMock()
    monkeypatch.setattr(graph_refresh, "run_pipeline", pipeline_spy)

    await graph_refresh.run_refresh(repo_id=repo_id, request_id="noop")

    pipeline_spy.assert_not_called()
    async with async_session() as s:
        result = await s.execute(select(RepoGraph).where(RepoGraph.repo_id == repo_id))
        row = result.scalar_one()
        assert row.is_complete is True
        assert row.commit_sha == "AAA"
        assert "x.py" in row.processed_files


@pytest.mark.asyncio
async def test_resume_diff_drops_changed_file_entries(monkeypatch, tmp_path):
    _skip_if_no_db()
    from agent.lifecycle import graph_refresh
    from agent.graph_analyzer.diff import ChangedFilesPlan

    async with async_session() as s:
        repo = Repo(name="cardamon-diff", url="...", default_branch="main",
                    organization_id=1)
        s.add(repo)
        await s.flush()
        s.add(RepoGraphConfig(
            repo_id=repo.id, organization_id=1, analysis_branch="main",
            analyser_version="", workspace_path=str(tmp_path / "ws"),
        ))
        s.add(RepoGraph(
            repo_id=repo.id, commit_sha="OLD", status="ok",
            analyser_version="v1",
            graph_json={
                "nodes": [
                    {"id": "x.py::foo", "file": "x.py"},
                    {"id": "y.py::bar", "file": "y.py"},
                ],
                "edges": [],
            },
            is_complete=True,
            processed_files={"x.py": {}, "y.py": {}},
            failed_sites=[],
        ))
        await s.commit()
        repo_id = repo.id

    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()

    monkeypatch.setattr(graph_refresh, "_prepare_workspace", AsyncMock())
    monkeypatch.setattr(graph_refresh, "_resolve_commit_sha", AsyncMock(return_value="NEW"))
    monkeypatch.setattr(
        graph_refresh, "changed_files",
        AsyncMock(return_value=ChangedFilesPlan(modified=["x.py"])),
    )
    monkeypatch.setattr(
        "agent.graph_workspace.graph_workspace_path",
        lambda repo_id: str(workspace),
    )

    async def fake_pipeline(*, on_file_checkpoint=None, initial_processed_files=None, **kwargs):
        assert "x.py" not in initial_processed_files
        assert "y.py" in initial_processed_files
        from types import SimpleNamespace
        return SimpleNamespace(
            model_dump=lambda mode=None: {"nodes": [], "edges": [], "areas": [],
                                           "public_symbols": [], "commit_sha": "NEW"},
            model_dump_json=lambda: '{"nodes":[],"edges":[],"areas":[],"public_symbols":[],"commit_sha":"NEW"}',
            areas=[],
            analyser_version="test",
        )
    monkeypatch.setattr(graph_refresh, "run_pipeline", fake_pipeline)

    await graph_refresh.run_refresh(repo_id=repo_id, request_id="diff")

    async with async_session() as s:
        result = await s.execute(select(RepoGraph).where(RepoGraph.repo_id == repo_id))
        row = result.scalar_one()
        assert row.is_complete is True
        assert row.commit_sha == "NEW"
