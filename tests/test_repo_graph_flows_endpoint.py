"""Tests for POST /repos/{repo_id}/graph/flows/recompute (Phase 1 capability-flow map)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Repo, RepoGraph

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(*, repo_id: int = 1):
    repo = MagicMock(spec=Repo)
    repo.id = repo_id
    repo.name = "demo"
    repo.organization_id = 1
    return repo


def _make_graph_row(*, repo_id: int = 1, is_complete: bool = True):
    """Minimal RepoGraphBlob dict with 3 function nodes and one http edge."""
    graph_json = {
        "commit_sha": "abc1234",
        "generated_at": "2026-05-01T00:00:00+00:00",
        "analyser_version": "1.0",
        "areas": [
            {"name": "app", "status": "ok"},
            {"name": "db", "status": "ok"},
        ],
        "nodes": [
            {
                "id": "app:create_user",
                "label": "create_user",
                "kind": "function",
                "file": "app.py",
                "line_start": 10,
                "line_end": 18,
                "area": "app",
                "parent": None,
                "decorators": [],
            },
            {
                "id": "db:session.commit",
                "label": "session.commit",
                "kind": "function",
                "file": "db.py",
                "line_start": 5,
                "line_end": 7,
                "area": "db",
                "parent": None,
                "decorators": [],
            },
            {
                "id": "app:list_users",
                "label": "list_users",
                "kind": "function",
                "file": "app.py",
                "line_start": 20,
                "line_end": 28,
                "area": "app",
                "parent": None,
                "decorators": [],
            },
        ],
        "edges": [
            {
                "source": "__http__",
                "target": "app:create_user",
                "kind": "http",
                "evidence": {"file": "app.py", "line": 10, "snippet": "POST /users"},
                "source_kind": "ast",
            },
            {
                "source": "app:create_user",
                "target": "db:session.commit",
                "kind": "calls",
                "evidence": {"file": "app.py", "line": 15, "snippet": "session.commit()"},
                "source_kind": "ast",
            },
            {
                "source": "__http__",
                "target": "app:list_users",
                "kind": "http",
                "evidence": {"file": "app.py", "line": 20, "snippet": "GET /users"},
                "source_kind": "ast",
            },
        ],
    }

    row = MagicMock(spec=RepoGraph)
    row.id = 1
    row.repo_id = repo_id
    row.graph_json = graph_json
    row.flow_json = None
    row.is_complete = is_complete
    return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
@patch("agent.graph_workspace.graph_workspace_path")
async def test_recompute_writes_flow_json_for_completed_graph(
    mock_ws_path,
    mock_get_repo,
) -> None:
    """Happy path: completed graph → derives flows → persists flow_json → 200."""
    from orchestrator.router import recompute_graph_flows

    repo = _make_repo()
    row = _make_graph_row(is_complete=True)

    mock_get_repo.return_value = repo
    # Workspace path does not exist so falls back to path-only hash mode.
    mock_ws_path.return_value = Path("/nonexistent/workspace")

    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = row
    session.execute.return_value = mock_result

    out = await recompute_graph_flows(
        repo_id=repo.id,
        session=session,
        org_id=1,
    )

    # Shape checks — endpoint now returns a typed RecomputeFlowsResponse.
    assert out.repo_id == repo.id
    assert isinstance(out.flow_count, int)
    assert isinstance(out.capability_count, int)
    assert isinstance(out.unreached_count, int)
    assert out.derived_at_commit == "abc1234"

    # At least one flow detected (two HTTP entry-points → two flows).
    assert out.flow_count >= 1

    # flow_json was persisted.
    assert row.flow_json is not None
    session.commit.assert_called_once()


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_recompute_404_when_no_completed_graph(
    mock_get_repo,
) -> None:
    """No completed RepoGraph row → 404 with informative message."""
    from orchestrator.router import recompute_graph_flows

    repo = _make_repo()
    mock_get_repo.return_value = repo

    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session.execute.return_value = mock_result

    with pytest.raises(HTTPException) as exc:
        await recompute_graph_flows(repo_id=repo.id, session=session, org_id=1)

    assert exc.value.status_code == 404
    # The message should guide the user to run /graph/refresh first.
    assert "refresh" in exc.value.detail.lower()


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_recompute_404_for_other_org_repo(
    mock_get_repo,
) -> None:
    """Cross-org access: _get_repo_in_org returns None → 404 (hides existence)."""
    from orchestrator.router import recompute_graph_flows

    # Simulates the cross-org case: _get_repo_in_org returns None.
    mock_get_repo.return_value = None

    session = AsyncMock(spec=AsyncSession)

    with pytest.raises(HTTPException) as exc:
        await recompute_graph_flows(repo_id=99, session=session, org_id=1)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_recompute_uses_workspace_when_exists(
    mock_get_repo,
    tmp_path: Path,
) -> None:
    """When graph_workspace_path returns a real existing directory, it is
    forwarded to derive_flow_blob as workspace_root (not None)."""
    from agent.graph_analyzer.flows import derive_flow_blob
    from orchestrator.router import recompute_graph_flows

    repo = _make_repo()
    row = _make_graph_row(is_complete=True)

    mock_get_repo.return_value = repo

    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = row
    session.execute.return_value = mock_result

    captured: dict[str, Any] = {}
    real_derive = derive_flow_blob

    def _spy(graph_blob, workspace_root=None):
        captured["workspace_root"] = workspace_root
        return real_derive(graph_blob, workspace_root=workspace_root)

    with (
        patch("agent.graph_analyzer.flows.derive_flow_blob", side_effect=_spy),
        patch("agent.graph_workspace.graph_workspace_path", return_value=tmp_path),
    ):
        out = await recompute_graph_flows(
            repo_id=repo.id,
            session=session,
            org_id=1,
        )

    assert captured["workspace_root"] == tmp_path
    assert out.repo_id == repo.id
    assert row.flow_json is not None


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_recompute_handles_str_return_from_graph_workspace_path(
    mock_get_repo,
    tmp_path: Path,
) -> None:
    """Regression: graph_workspace_path returns str, endpoint must Path() it
    before calling .exists(). A str has no .exists() → AttributeError if
    the endpoint doesn't wrap it in Path first."""
    from orchestrator.router import recompute_graph_flows

    repo = _make_repo()
    row = _make_graph_row(is_complete=True)

    mock_get_repo.return_value = repo

    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = row
    session.execute.return_value = mock_result

    # Return a plain str (as the real graph_workspace_path does via os.path.join)
    # pointing at a non-existent path, so the endpoint falls back to path-only
    # hash mode. The key assertion: no AttributeError from calling .exists() on str.
    with patch(
        "agent.graph_workspace.graph_workspace_path",
        return_value=str(tmp_path / "nonexistent"),
    ):
        out = await recompute_graph_flows(
            repo_id=repo.id,
            session=session,
            org_id=1,
        )

    assert out.repo_id == repo.id
    assert isinstance(out.flow_count, int)
    assert row.flow_json is not None
    session.commit.assert_called_once()
