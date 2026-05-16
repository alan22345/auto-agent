"""Code-graph refresh event handler (ADR-016 Phase 2).

Subscribed against ``RepoEventType.GRAPH_REQUESTED`` in
``agent/main.py``. Mirrors the ``harness_onboard`` pattern — a thin
event-shape adapter delegating to a richer implementation here.

What the handler does:

1. Load the ``RepoGraphConfig`` (+ parent ``Repo``) for the requested
   repo. A missing config means somebody disabled the graph between the
   request and the dispatch — log + return, do not error out.
2. Acquire the per-repo ``graph_workspace_lock``. If the lock can't be
   acquired before its timeout, publish
   ``repo_graph_failed(error="analysis already running")`` and stop.
3. Clone (first run) or fetch+reset (subsequent runs) the workspace.
4. Resolve ``commit_sha`` from ``git rev-parse HEAD``.
5. Run the tree-sitter pipeline (``agent.graph_analyzer.run_pipeline``)
   against the workspace and assemble a ``RepoGraphBlob``.
6. Write a ``RepoGraph`` row, update
   ``RepoGraphConfig.last_analysis_id`` + ``analyser_version``, commit.
7. Publish ``repo_graph_ready``.

Catastrophic failures (clone failed, lock timeout, DB write) publish
``repo_graph_failed``. Per-area parser failures are NOT catastrophic —
they're surfaced as ``AreaStatus.failed`` inside the blob and the row's
``status`` is set to ``partial``.
"""

from __future__ import annotations

import asyncio
import json
import os

import structlog
from sqlalchemy import select

from agent.graph_analyzer import run_pipeline
from agent.graph_analyzer.pipeline import overall_status
from agent.graph_workspace import (
    GraphWorkspaceLockTimeout,
    graph_workspace_lock,
)
from agent.llm import get_provider
from shared.database import async_session
from shared.events import (
    Event,
    publish,
    repo_graph_failed,
    repo_graph_ready,
)
from shared.models import Repo, RepoGraph, RepoGraphConfig

log = structlog.get_logger(__name__)

# Lock timeout used here is shorter than the analyser's worst-case
# runtime — if another analyser already holds the lock, we fail fast
# with "already running" rather than queueing behind it.
_LOCK_TIMEOUT_SECONDS = 5.0


async def handle(event: Event) -> None:
    """EventBus entry — adapt the ``Event`` to the analyser call."""
    payload = event.payload or {}
    repo_id = payload.get("repo_id")
    request_id = payload.get("request_id", "")
    if not isinstance(repo_id, int):
        log.warning("graph_refresh_invalid_payload", payload=payload)
        return
    await run_refresh(repo_id=repo_id, request_id=request_id)


async def run_refresh(*, repo_id: int, request_id: str) -> None:
    """Run the graph refresh end-to-end for ``repo_id``.

    Public for tests so they can drive the analyser without faking an
    ``Event`` object.
    """
    async with async_session() as session:
        cfg = await _load_config(session, repo_id=repo_id)
        if cfg is None:
            log.info("graph_refresh_config_missing", repo_id=repo_id)
            return
        repo = await _load_repo(session, repo_id=repo_id)
        if repo is None:
            log.info("graph_refresh_repo_missing", repo_id=repo_id)
            return

    workspace = cfg.workspace_path
    branch = cfg.analysis_branch
    repo_url = repo.url

    try:
        async with graph_workspace_lock(
            repo_id=repo_id,
            timeout=_LOCK_TIMEOUT_SECONDS,
        ):
            await _prepare_workspace(
                workspace=workspace,
                repo_url=repo_url,
                branch=branch,
            )
            commit_sha = await _resolve_commit_sha(workspace=workspace)
            blob = await run_pipeline(
                workspace=workspace,
                commit_sha=commit_sha,
                provider=get_provider(),
            )
    except GraphWorkspaceLockTimeout:
        log.warning(
            "graph_refresh_lock_busy",
            repo_id=repo_id,
            request_id=request_id,
        )
        await publish(
            repo_graph_failed(
                repo_id=repo_id,
                error="analysis already running",
            )
        )
        return
    except Exception as e:
        log.exception(
            "graph_refresh_catastrophic_failure",
            repo_id=repo_id,
            request_id=request_id,
            error=str(e),
        )
        await publish(
            repo_graph_failed(
                repo_id=repo_id,
                error=str(e) or e.__class__.__name__,
            )
        )
        return

    # Persist the row + update the config. Wrapping the DB writes in
    # their own try/except so a transient DB error still surfaces a
    # FAILED event to the user.
    try:
        async with async_session() as session:
            status = overall_status(blob.areas)
            row = RepoGraph(
                repo_id=repo_id,
                commit_sha=blob.commit_sha,
                generated_at=blob.generated_at,
                analyser_version=blob.analyser_version,
                status=status,
                # SQLAlchemy + JSONB happily accept a dict; we go via
                # ``model_dump(mode="json")`` so datetime is serialised
                # to ISO-8601 strings the way the wire expects.
                graph_json=json.loads(blob.model_dump_json()),
            )
            session.add(row)
            await session.flush()

            cfg_row = await _load_config(session, repo_id=repo_id)
            if cfg_row is not None:
                cfg_row.last_analysis_id = row.id
                cfg_row.analyser_version = blob.analyser_version
            await session.commit()
            await session.refresh(row)
            repo_graph_id = row.id
    except Exception as e:
        log.exception(
            "graph_refresh_db_write_failed",
            repo_id=repo_id,
            error=str(e),
        )
        await publish(
            repo_graph_failed(
                repo_id=repo_id,
                error=f"database write failed: {e}",
            )
        )
        return

    await publish(
        repo_graph_ready(
            repo_id=repo_id,
            repo_graph_id=repo_graph_id,
            commit_sha=blob.commit_sha,
            status=status,
        )
    )


# ----------------------------------------------------------------------
# DB helpers
# ----------------------------------------------------------------------


async def _load_config(session, *, repo_id: int) -> RepoGraphConfig | None:
    result = await session.execute(
        select(RepoGraphConfig).where(RepoGraphConfig.repo_id == repo_id)
    )
    return result.scalar_one_or_none()


async def _load_repo(session, *, repo_id: int) -> Repo | None:
    result = await session.execute(select(Repo).where(Repo.id == repo_id))
    return result.scalar_one_or_none()


# ----------------------------------------------------------------------
# Git helpers — subprocess wrappers
# ----------------------------------------------------------------------


async def _prepare_workspace(*, workspace: str, repo_url: str, branch: str) -> None:
    """Clone the workspace on first run, or refresh it on subsequent runs.

    ADR-016 §9: refresh uses ``git fetch origin && git reset --hard
    origin/<branch>`` — safer than ``git pull`` against force-pushed
    branches. First run uses ``git clone --depth=1 -b <branch>``.
    """
    os.makedirs(os.path.dirname(workspace) or ".", exist_ok=True)

    if not os.path.isdir(os.path.join(workspace, ".git")):
        # Clean any partial leftover so clone has a fresh target.
        if os.path.exists(workspace):
            import shutil

            shutil.rmtree(workspace, ignore_errors=True)
        await _run_git(
            args=["clone", "--depth=1", "-b", branch, repo_url, workspace],
            cwd=None,
        )
        return

    await _run_git(args=["fetch", "origin"], cwd=workspace)
    await _run_git(
        args=["reset", "--hard", f"origin/{branch}"],
        cwd=workspace,
    )


async def _resolve_commit_sha(*, workspace: str) -> str:
    return (await _run_git(args=["rev-parse", "HEAD"], cwd=workspace)).strip()


async def _run_git(*, args: list[str], cwd: str | None) -> str:
    """Run ``git <args>``; raise on non-zero exit.

    Centralised here so tests can monkeypatch one symbol to stub out
    every git call the handler makes.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {err}")
    return stdout.decode("utf-8", errors="replace")


__all__ = ["handle", "run_refresh"]
