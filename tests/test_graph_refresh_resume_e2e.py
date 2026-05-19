"""End-to-end: refresh, cancel mid-pipeline, refresh again, assert resume.

Uses mocks for git + LLM provider (mirrors tests/test_graph_refresh_handler.py).
DB-backed — skips when DATABASE_URL is missing."""

from __future__ import annotations

import asyncio
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
async def test_resume_after_midflight_cancel(monkeypatch, tmp_path):
    _skip_if_no_db()
    from agent.lifecycle import graph_refresh

    async with async_session() as s:
        repo = Repo(name="cardamon-e2e", url="https://github.com/x/y",
                    default_branch="main", organization_id=1)
        s.add(repo)
        await s.flush()
        cfg = RepoGraphConfig(
            repo_id=repo.id, organization_id=1, analysis_branch="main",
            analyser_version="", workspace_path=str(tmp_path / "ws"),
        )
        s.add(cfg)
        await s.commit()
        repo_id = repo.id

    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()

    monkeypatch.setattr(graph_refresh, "_run_git", AsyncMock(return_value="cafebabe\n"))
    monkeypatch.setattr(graph_refresh, "_prepare_workspace", AsyncMock())
    monkeypatch.setattr(graph_refresh, "_resolve_commit_sha", AsyncMock(return_value="cafebabe"))
    monkeypatch.setattr(
        "agent.graph_workspace.graph_workspace_path",
        lambda repo_id: str(workspace),
    )

    call_count = {"flushed": 0}

    async def fake_pipeline(*, on_file_checkpoint=None, **kwargs):
        await on_file_checkpoint(
            {"nodes": [{"id": "a.ts::x", "file": "a.ts"}], "edges": []},
            {"a.ts": {"sites_attempted": 1, "sites_succeeded": 1,
                      "edges_added": 0, "processed_at": "2026-05-19T00:00:00Z"}},
            [],
        )
        call_count["flushed"] += 1
        await on_file_checkpoint(
            {"nodes": [{"id": "a.ts::x", "file": "a.ts"},
                       {"id": "b.ts::y", "file": "b.ts"}],
             "edges": []},
            {"a.ts": {"sites_attempted": 1, "sites_succeeded": 1,
                      "edges_added": 0, "processed_at": "2026-05-19T00:00:00Z"},
             "b.ts": {"sites_attempted": 1, "sites_succeeded": 1,
                      "edges_added": 0, "processed_at": "2026-05-19T00:00:01Z"}},
            [],
        )
        call_count["flushed"] += 1
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(graph_refresh, "run_pipeline", fake_pipeline)

    with pytest.raises(RuntimeError):
        await graph_refresh.run_refresh(repo_id=repo_id, request_id="r1")

    async with async_session() as s:
        result = await s.execute(select(RepoGraph).where(RepoGraph.repo_id == repo_id))
        row = result.scalar_one()
        assert row.is_complete is False
        assert set(row.processed_files.keys()) == {"a.ts", "b.ts"}

    async def fake_pipeline_2(*, on_file_checkpoint=None, initial_processed_files=None, **kwargs):
        assert set(initial_processed_files.keys()) == {"a.ts", "b.ts"}
        await on_file_checkpoint(
            {"nodes": [{"id": "c.ts::z", "file": "c.ts"}], "edges": []},
            {**initial_processed_files,
             "c.ts": {"sites_attempted": 1, "sites_succeeded": 1,
                      "edges_added": 0, "processed_at": "2026-05-19T00:00:02Z"}},
            [],
        )
        from types import SimpleNamespace
        return SimpleNamespace(
            model_dump=lambda mode=None: {
                "nodes": [], "edges": [], "areas": [], "public_symbols": [],
                "commit_sha": "cafebabe",
            },
            model_dump_json=lambda: '{"nodes":[],"edges":[],"areas":[],"public_symbols":[],"commit_sha":"cafebabe"}',
            areas=[],
            analyser_version="test",
        )

    monkeypatch.setattr(graph_refresh, "run_pipeline", fake_pipeline_2)
    await graph_refresh.run_refresh(repo_id=repo_id, request_id="r2")

    async with async_session() as s:
        result = await s.execute(select(RepoGraph).where(RepoGraph.repo_id == repo_id))
        row = result.scalar_one()
        assert row.is_complete is True
        assert "c.ts" in row.processed_files
