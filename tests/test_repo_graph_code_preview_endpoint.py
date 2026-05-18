"""Side-panel code-preview endpoint tests (ADR-016 Phase 7 §11).

Covers:

* Happy path returns the requested window verbatim.
* 404 when the file is missing in the workspace.
* 400 when the line range exceeds the per-call cap.
* 400 when the range is malformed (line_end < line_start).
* 422 when the path tries to traverse out of the workspace.
* 422 when the resolved path escapes the workspace via a symlink.
"""

from __future__ import annotations

import os
from pathlib import Path
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


def _make_config(*, workspace_path: str, repo_id: int = 1):
    cfg = MagicMock(spec=RepoGraphConfig)
    cfg.repo_id = repo_id
    cfg.organization_id = 1
    cfg.analysis_branch = "main"
    cfg.workspace_path = workspace_path
    return cfg


def _session_with_cfg(cfg: object | None) -> AsyncSession:
    session = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = cfg
    session.execute.return_value = mock_result
    return session


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_code_preview_happy_path(
    mock_get_repo,
    tmp_path: Path,
) -> None:
    from orchestrator.router import get_graph_code_preview

    workspace = tmp_path / "ws"
    workspace.mkdir()
    src = workspace / "agent" / "dog.py"
    src.parent.mkdir(parents=True)
    src.write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n")

    mock_get_repo.return_value = _make_repo()
    cfg = _make_config(workspace_path=str(workspace))
    session = _session_with_cfg(cfg)

    out = await get_graph_code_preview(
        repo_id=1,
        path="agent/dog.py",
        line_start=3,
        line_end=5,
        session=session,
        org_id=1,
    )

    assert out.file == "agent/dog.py"
    assert out.line_start == 3
    assert out.line_end == 5
    assert out.content == "line 3\nline 4\nline 5\n"


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_code_preview_returns_404_when_file_missing(
    mock_get_repo,
    tmp_path: Path,
) -> None:
    from orchestrator.router import get_graph_code_preview

    workspace = tmp_path / "ws"
    workspace.mkdir()

    mock_get_repo.return_value = _make_repo()
    cfg = _make_config(workspace_path=str(workspace))
    session = _session_with_cfg(cfg)

    with pytest.raises(HTTPException) as exc:
        await get_graph_code_preview(
            repo_id=1,
            path="agent/missing.py",
            line_start=1,
            line_end=10,
            session=session,
            org_id=1,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_code_preview_rejects_oversized_range(
    mock_get_repo,
    tmp_path: Path,
) -> None:
    from orchestrator.router import (
        GRAPH_CODE_PREVIEW_MAX_LINES,
        get_graph_code_preview,
    )

    workspace = tmp_path / "ws"
    workspace.mkdir()
    src = workspace / "huge.py"
    src.write_text("x\n" * 10_000)

    mock_get_repo.return_value = _make_repo()
    cfg = _make_config(workspace_path=str(workspace))
    session = _session_with_cfg(cfg)

    with pytest.raises(HTTPException) as exc:
        await get_graph_code_preview(
            repo_id=1,
            path="huge.py",
            line_start=1,
            line_end=GRAPH_CODE_PREVIEW_MAX_LINES + 1,
            session=session,
            org_id=1,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_code_preview_rejects_inverted_range(
    mock_get_repo,
    tmp_path: Path,
) -> None:
    from orchestrator.router import get_graph_code_preview

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "f.py").write_text("a\nb\nc\n")

    mock_get_repo.return_value = _make_repo()
    cfg = _make_config(workspace_path=str(workspace))
    session = _session_with_cfg(cfg)

    with pytest.raises(HTTPException) as exc:
        await get_graph_code_preview(
            repo_id=1,
            path="f.py",
            line_start=10,
            line_end=5,
            session=session,
            org_id=1,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_code_preview_rejects_dotdot_traversal(
    mock_get_repo,
    tmp_path: Path,
) -> None:
    from orchestrator.router import get_graph_code_preview

    workspace = tmp_path / "ws"
    workspace.mkdir()
    # Sibling file outside the workspace — must not be readable.
    sibling = tmp_path / "secrets.txt"
    sibling.write_text("super secret")

    mock_get_repo.return_value = _make_repo()
    cfg = _make_config(workspace_path=str(workspace))
    session = _session_with_cfg(cfg)

    with pytest.raises(HTTPException) as exc:
        await get_graph_code_preview(
            repo_id=1,
            path="../secrets.txt",
            line_start=1,
            line_end=1,
            session=session,
            org_id=1,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_code_preview_rejects_absolute_path(
    mock_get_repo,
    tmp_path: Path,
) -> None:
    from orchestrator.router import get_graph_code_preview

    workspace = tmp_path / "ws"
    workspace.mkdir()

    mock_get_repo.return_value = _make_repo()
    cfg = _make_config(workspace_path=str(workspace))
    session = _session_with_cfg(cfg)

    with pytest.raises(HTTPException) as exc:
        await get_graph_code_preview(
            repo_id=1,
            path="/etc/passwd",
            line_start=1,
            line_end=1,
            session=session,
            org_id=1,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_code_preview_rejects_symlink_escape(
    mock_get_repo,
    tmp_path: Path,
) -> None:
    """A symlink inside the workspace pointing outside must not be
    served — the realpath check is the final line of defence."""
    from orchestrator.router import get_graph_code_preview

    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("hidden")
    # Symlink inside the workspace pointing outside it.
    link = workspace / "escape.txt"
    os.symlink(outside, link)

    mock_get_repo.return_value = _make_repo()
    cfg = _make_config(workspace_path=str(workspace))
    session = _session_with_cfg(cfg)

    with pytest.raises(HTTPException) as exc:
        await get_graph_code_preview(
            repo_id=1,
            path="escape.txt",
            line_start=1,
            line_end=1,
            session=session,
            org_id=1,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_code_preview_returns_404_when_repo_missing(
    mock_get_repo,
    tmp_path: Path,
) -> None:
    from orchestrator.router import get_graph_code_preview

    mock_get_repo.return_value = None
    session = AsyncMock(spec=AsyncSession)

    with pytest.raises(HTTPException) as exc:
        await get_graph_code_preview(
            repo_id=99,
            path="agent/dog.py",
            line_start=1,
            line_end=5,
            session=session,
            org_id=1,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@patch("orchestrator.router._get_repo_in_org", new_callable=AsyncMock)
async def test_code_preview_returns_404_when_graph_not_enabled(
    mock_get_repo,
    tmp_path: Path,
) -> None:
    from orchestrator.router import get_graph_code_preview

    mock_get_repo.return_value = _make_repo()
    session = _session_with_cfg(None)

    with pytest.raises(HTTPException) as exc:
        await get_graph_code_preview(
            repo_id=1,
            path="agent/dog.py",
            line_start=1,
            line_end=5,
            session=session,
            org_id=1,
        )
    assert exc.value.status_code == 404
