"""Tests for GET /repos/{repo_id}/graph/flows and the recompute alignment fix.

Phase 3 of the capability-flow map spec adds a read endpoint backing the
Map view, and aligns recompute writes with ``RepoGraphConfig.last_analysis_id``
so the agent op ``which_capability`` and the UI see the same row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import Repo, RepoGraph, RepoGraphConfig


def _make_repo(*, repo_id: int = 1):
    repo = MagicMock(spec=Repo)
    repo.id = repo_id
    repo.name = "demo"
    repo.organization_id = 1
    return repo


def _flow_json_blob_dict() -> dict:
    """Minimal valid FlowJsonBlob payload."""
    return {
        "capabilities": [
            {
                "id": "cap_0",
                "flow_ids": ["flow_a"],
                "flow_membership_hash": "sha256:cafe",
                "name": "Auth",
                "description": "Login",
                "labeled_at_commit": "abc1234",
            }
        ],
        "flows": [
            {
                "id": "flow_a",
                "entry_point": {"node_id": "app:login", "kind": "http"},
                "terminal_node_id": "db:write",
                "terminal_kind": "db_write",
                "steps": [
                    {
                        "node_id": "app:login",
                        "depth": 0,
                        "is_branch_root": False,
                        "is_cycle_back": False,
                    }
                ],
                "file_set": ["app.py"],
                "file_set_hash": "sha256:beef",
                "name": "Login",
                "description": "User login",
                "labeled_at_commit": "abc1234",
            }
        ],
        "unreached": ["lib:noop"],
        "derived_at_commit": "abc1234",
        "deriver_version": "phase1",
        "labeler_model": "claude-haiku-4-5",
    }


def _make_graph_row(*, repo_graph_id: int = 7, with_flow_json: bool = True):
    row = MagicMock(spec=RepoGraph)
    row.id = repo_graph_id
    row.flow_json = _flow_json_blob_dict() if with_flow_json else None
    row.generated_at = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)
    return row


def _make_cfg(*, last_analysis_id: int | None = 7):
    cfg = MagicMock(spec=RepoGraphConfig)
    cfg.last_analysis_id = last_analysis_id
    return cfg


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_get_flows_returns_blob_when_present(mock_get_repo) -> None:
    """Happy path: config + analysis row with flow_json → 200 + blob."""
    from orchestrator.router import get_repo_graph_flows

    repo = _make_repo()
    cfg = _make_cfg(last_analysis_id=7)
    row = _make_graph_row(repo_graph_id=7, with_flow_json=True)

    mock_get_repo.return_value = repo

    session = AsyncMock(spec=AsyncSession)
    results = [MagicMock(), MagicMock()]
    results[0].scalar_one_or_none.return_value = cfg
    results[1].scalar_one_or_none.return_value = row
    session.execute.side_effect = results

    out = await get_repo_graph_flows(repo_id=repo.id, session=session, org_id=1)

    assert out.repo_id == repo.id
    assert out.repo_graph_id == 7
    assert out.blob is not None
    assert out.blob.derived_at_commit == "abc1234"
    assert len(out.blob.capabilities) == 1
    assert len(out.blob.flows) == 1


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_get_flows_null_blob_when_not_computed(mock_get_repo) -> None:
    """Config exists, analysis row exists, but no flow_json yet → blob=None."""
    from orchestrator.router import get_repo_graph_flows

    repo = _make_repo()
    cfg = _make_cfg(last_analysis_id=7)
    row = _make_graph_row(repo_graph_id=7, with_flow_json=False)

    mock_get_repo.return_value = repo

    session = AsyncMock(spec=AsyncSession)
    results = [MagicMock(), MagicMock()]
    results[0].scalar_one_or_none.return_value = cfg
    results[1].scalar_one_or_none.return_value = row
    session.execute.side_effect = results

    out = await get_repo_graph_flows(repo_id=repo.id, session=session, org_id=1)

    assert out.repo_id == repo.id
    assert out.repo_graph_id == 7
    assert out.blob is None


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_get_flows_null_blob_when_no_analysis(mock_get_repo) -> None:
    """Config exists but last_analysis_id is None → blob=None, no row lookup."""
    from orchestrator.router import get_repo_graph_flows

    repo = _make_repo()
    cfg = _make_cfg(last_analysis_id=None)

    mock_get_repo.return_value = repo

    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = cfg
    session.execute.return_value = result

    out = await get_repo_graph_flows(repo_id=repo.id, session=session, org_id=1)

    assert out.repo_id == repo.id
    assert out.repo_graph_id is None
    assert out.blob is None


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_get_flows_404_when_graph_not_enabled(mock_get_repo) -> None:
    """No RepoGraphConfig row → 404 with helpful message."""
    from orchestrator.router import get_repo_graph_flows

    repo = _make_repo()
    mock_get_repo.return_value = repo

    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result

    with pytest.raises(HTTPException) as exc:
        await get_repo_graph_flows(repo_id=repo.id, session=session, org_id=1)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_get_flows_404_when_repo_not_in_org(mock_get_repo) -> None:
    """Cross-org access: _get_repo_in_org returns None → 404 (hides existence)."""
    from orchestrator.router import get_repo_graph_flows

    mock_get_repo.return_value = None
    session = AsyncMock(spec=AsyncSession)

    with pytest.raises(HTTPException) as exc:
        await get_repo_graph_flows(repo_id=99, session=session, org_id=1)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_get_flows_tolerates_stale_flow_json_shape(mock_get_repo) -> None:
    """Old/invalid flow_json shape → blob=None (don't crash the GET)."""
    from orchestrator.router import get_repo_graph_flows

    repo = _make_repo()
    cfg = _make_cfg(last_analysis_id=7)
    row = MagicMock(spec=RepoGraph)
    row.id = 7
    row.flow_json = {"invalid": "shape", "missing": "required_fields"}
    row.generated_at = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)

    mock_get_repo.return_value = repo

    session = AsyncMock(spec=AsyncSession)
    results = [MagicMock(), MagicMock()]
    results[0].scalar_one_or_none.return_value = cfg
    results[1].scalar_one_or_none.return_value = row
    session.execute.side_effect = results

    out = await get_repo_graph_flows(repo_id=repo.id, session=session, org_id=1)

    assert out.repo_graph_id == 7
    assert out.blob is None


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
@patch("agent.graph_workspace.graph_workspace_path")
async def test_recompute_updates_last_analysis_id_to_recomputed_row(
    mock_ws_path,
    mock_get_repo,
) -> None:
    """Alignment fix: recompute writes flow_json to a row AND bumps
    ``RepoGraphConfig.last_analysis_id`` to that row's id so the GET
    endpoint + agent op see the same row."""
    from pathlib import Path

    from orchestrator.router import recompute_graph_flows

    repo = _make_repo()
    mock_get_repo.return_value = repo
    mock_ws_path.return_value = Path("/nonexistent/workspace")

    # The recompute row (latest is_complete=True ordered by generated_at) has id=99.
    row = MagicMock(spec=RepoGraph)
    row.id = 99
    row.flow_json = None
    row.graph_json = {
        "commit_sha": "abc1234",
        "generated_at": "2026-05-01T00:00:00+00:00",
        "analyser_version": "1.0",
        "areas": [{"name": "app", "status": "ok"}],
        "nodes": [
            {
                "id": "app:login",
                "label": "login",
                "kind": "function",
                "file": "app.py",
                "line_start": 1,
                "line_end": 5,
                "area": "app",
                "parent": None,
                "decorators": [],
            }
        ],
        "edges": [],
    }
    row.is_complete = True

    # Config previously pointed at an OLDER row (id=42).
    cfg = _make_cfg(last_analysis_id=42)

    session = AsyncMock(spec=AsyncSession)
    results = [MagicMock(), MagicMock()]
    results[0].scalar_one_or_none.return_value = row  # RepoGraph lookup
    results[1].scalar_one_or_none.return_value = cfg  # RepoGraphConfig lookup
    session.execute.side_effect = results

    async def _passthrough_labeller(blob, **kw):
        return blob

    with (
        patch(
            "agent.graph_analyzer.flow_labeler.label_flow_blob",
            side_effect=_passthrough_labeller,
        ),
        patch("agent.llm.get_structured_extractor_provider", return_value=MagicMock()),
    ):
        await recompute_graph_flows(repo_id=repo.id, session=session, org_id=1)

    # Alignment fix: cfg.last_analysis_id is now the recompute row's id (99),
    # not the stale 42.
    assert cfg.last_analysis_id == 99
    assert row.flow_json is not None
    session.commit.assert_called_once()
