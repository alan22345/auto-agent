"""Debounced code-graph refresh on repo change (ADR-024).

The graph goes stale the moment its analysis branch moves on origin.
The merge paths that move it — the PR-merged webhook, push webhooks,
and auto-agent's own ``_auto_merge_pr`` — call
:func:`request_graph_refresh_soon` fire-and-forget. After a quiet
period one ``repo.graph_requested`` event is published and the
existing refresh handler (``agent/lifecycle/graph_refresh.py``) does
the rest, per-repo flock included.

Guarantees the triggers rely on:

* **Best-effort, never raises** — a graph refresh must never break a
  merge path. Every failure is logged and swallowed.
* **Bursts collapse** — repeated calls within the window reset the
  timer; one event fires per quiet repo.
* **Only the analysed branch counts** — pass ``branch`` when the
  changed branch is known; a mismatch with the repo's
  ``analysis_branch`` skips the refresh.
* **Only maintained graphs refresh** — repos without a completed
  analysis are ignored; the first analysis stays an explicit
  onboarding action.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog
from sqlalchemy import select

from shared.database import async_session
from shared.events import publish, repo_graph_requested
from shared.models import RepoGraphConfig

log = structlog.get_logger(__name__)

# Long enough to absorb a burst of merges, short enough that the graph
# is current again within a minute of the repo changing.
DEFAULT_DEBOUNCE_SECONDS = 30.0

_pending: dict[int, asyncio.Task] = {}


def request_graph_refresh_soon(
    repo_id: int,
    *,
    branch: str | None = None,
    delay_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
) -> None:
    """Schedule a graph refresh once ``repo_id`` goes quiet.

    ``branch`` is the branch that changed, when the caller knows it;
    the refresh only fires if it matches the repo's analysis branch
    (``None`` skips that check). Repeated calls within the window
    supersede the pending one — trailing-edge debounce.
    """
    pending = _pending.get(repo_id)
    if pending is not None and not pending.done():
        pending.cancel()
    _pending[repo_id] = asyncio.create_task(
        _request_after_quiet_period(repo_id, branch, delay_seconds),
    )


async def _request_after_quiet_period(
    repo_id: int,
    branch: str | None,
    delay_seconds: float,
) -> None:
    try:
        await asyncio.sleep(delay_seconds)
        analysis_branch = await _analysis_branch_for(repo_id)
        if analysis_branch is None:
            return  # graph not enabled, or never analysed — not ours to start
        if branch is not None and branch != analysis_branch:
            return  # the analysed branch didn't move
        await publish(
            repo_graph_requested(repo_id=repo_id, request_id=str(uuid.uuid4())),
        )
        log.info(
            "graph_refresh_requested_on_change",
            repo_id=repo_id,
            branch=branch,
        )
    except asyncio.CancelledError:
        raise  # superseded by a newer change in the window
    except Exception as e:
        log.warning(
            "graph_refresh_request_failed",
            repo_id=repo_id,
            error=str(e),
        )


async def _analysis_branch_for(repo_id: int) -> str | None:
    """The repo's analysed branch, or None when no completed graph exists."""
    async with async_session() as session:
        result = await session.execute(
            select(RepoGraphConfig).where(RepoGraphConfig.repo_id == repo_id),
        )
        cfg = result.scalar_one_or_none()
        if cfg is None or cfg.last_analysis_id is None:
            return None
        return cfg.analysis_branch


__all__ = ["DEFAULT_DEBOUNCE_SECONDS", "request_graph_refresh_soon"]
