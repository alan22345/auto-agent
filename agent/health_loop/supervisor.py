"""Auto-heal supervisor — the long-lived loop that drains health findings.

Modeled on ``agent/po_analyzer.py::run_po_analysis_loop``: a background async
loop started in ``run.py``. Each tick reconciles the VM-global lease and, while
a repo's loop is active, drains exactly ONE batch (the batch handler runs the
fix inline). Serial by construction — one batch per tick, the next tick only
after the current one reaches a terminal state — which is also where a Stop
takes effect (state is re-read every tick).

Lease lifecycle:
  - A repo's loop is "active" while its config is ``enabled`` and not
    ``paused`` (states ``running``/``idle``). Active ⇒ it holds the lease, so
    the dispatcher blocks all OTHER task dispatch (orchestrator/queue.py).
  - Only ONE repo's loop is active VM-wide (single lease key); a second
    enabled repo waits its turn.
  - The lease is renewed in the background while a (possibly long) batch runs.
  - Stop (state→paused) releases the lease at the next tick, after the
    in-flight batch has finished. Crash ⇒ the TTL frees the lease so the VM
    isn't wedged; state is re-derived from the config on restart.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from agent.health_loop.batch_handler import run_batch
from agent.health_loop.cleanup_branch import DEFAULT_CLEANUP_BRANCH
from agent.health_loop.config_service import (
    get_addressed,
    get_config,
    get_suppressed,
    list_active_configs,
    mark_addressed,
    record_outcome,
    set_cleanup_pr_url,
    set_current_batch,
    set_state,
)
from agent.health_loop.findings import select_batch
from agent.health_loop.lease import (
    acquire_lease,
    lease_holder,
    release_lease,
    renew_lease,
)
from agent.po_graph_findings import load_latest_graph_blob
from shared.database import async_session
from shared.models import Repo

log = structlog.get_logger()

CHECK_INTERVAL = 60  # seconds between ticks (idle poll + inter-batch gap)
LEASE_TTL = 1800  # 30 min — comfortably outlasts any single tick's idle gap
RENEW_INTERVAL = 600  # renew every 10 min while a batch runs

_HOLDER_PREFIX = "health-loop:"


def _holder(repo_id: int) -> str:
    return f"{_HOLDER_PREFIX}{repo_id}"


def _repo_from_holder(holder: str) -> int | None:
    try:
        return int(holder.removeprefix(_HOLDER_PREFIX))
    except ValueError:
        return None


async def run_health_loop_supervisor() -> None:
    """Background loop — reconcile the lease and drain one batch per tick."""
    log.info("health_loop.supervisor_started")
    while True:
        try:
            await _tick()
        except Exception as e:
            import traceback as _tb

            log.error(
                "health_loop.tick_error",
                error=str(e),
                error_type=type(e).__name__,
                tb=_tb.format_exc(),
            )
        await asyncio.sleep(CHECK_INTERVAL)


async def _tick() -> None:
    holder = await lease_holder()

    # Case 1 — a health loop already holds the lease.
    if holder and holder.startswith(_HOLDER_PREFIX):
        repo_id = _repo_from_holder(holder)
        cfg = await get_config(repo_id) if repo_id is not None else None
        if cfg is None or not cfg.enabled or cfg.state == "paused":
            # Stopped or disabled — release so normal dispatch resumes.
            await release_lease(holder)
            log.info("health_loop.lease_released", repo_id=repo_id, holder=holder)
            if cfg is not None and cfg.enabled and cfg.state != "paused":
                await set_state(repo_id, "paused")
            return
        await _drain_under_lease(holder, repo_id)
        return

    # Case 2 — the lease is held by something else entirely (shouldn't happen;
    # the key is health-loop-only). Leave it alone and retry next tick.
    if holder is not None:
        return

    # Case 3 — lease free. Acquire it for the next enabled, non-paused repo.
    cfg = await _next_runnable_config()
    if cfg is None:
        return
    holder = _holder(cfg.repo_id)
    if not await acquire_lease(holder, ttl_seconds=LEASE_TTL):
        return  # lost the race; try next tick
    log.info("health_loop.lease_acquired", repo_id=cfg.repo_id, holder=holder)
    await set_state(cfg.repo_id, "running")
    await _drain_under_lease(holder, cfg.repo_id)


async def _next_runnable_config():
    """First enabled config whose state wants the lease (running/idle)."""
    configs = await list_active_configs()
    runnable = [c for c in configs if c.state != "paused"]
    # Prefer one already 'running' so an in-progress repo keeps priority.
    runnable.sort(key=lambda c: (c.state != "running", c.repo_id))
    return runnable[0] if runnable else None


async def _drain_under_lease(holder: str, repo_id: int) -> None:
    """Drain one batch while keeping the lease renewed in the background."""
    renewer = asyncio.create_task(_renew_forever(holder))
    try:
        await _drain_one_batch(repo_id)
    finally:
        renewer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewer


async def _renew_forever(holder: str) -> None:
    while True:
        await asyncio.sleep(RENEW_INTERVAL)
        if not await renew_lease(holder, ttl_seconds=LEASE_TTL):
            log.warning("health_loop.lease_lost_mid_batch", holder=holder)
            return


async def _drain_one_batch(repo_id: int) -> None:
    """Select and run exactly one batch for ``repo_id`` (idle when exhausted)."""
    cfg = await get_config(repo_id)
    if cfg is None:
        return
    repo = await _load_repo(repo_id)
    blob = await load_latest_graph_blob(repo_id)
    if repo is None or blob is None:
        log.info(
            "health_loop.idle",
            repo_id=repo_id,
            reason="no_repo" if repo is None else "no_graph_blob",
        )
        await _go_idle(repo_id)
        return

    excluded = (await get_suppressed(repo_id)) | (await get_addressed(repo_id))
    batch = select_batch(blob, suppressed=excluded, in_flight=set(), batch_size=cfg.batch_size)
    if not batch:
        log.info("health_loop.idle", repo_id=repo_id, reason="no_eligible_findings")
        await _go_idle(repo_id)
        return

    log.info(
        "health_loop.batch_selected",
        repo_id=repo_id,
        count=len(batch),
        titles=[f.title[:80] for f in batch],
    )
    await set_state(repo_id, "running")
    await set_current_batch(repo_id, [{"hash": f.finding_hash, "title": f.title} for f in batch])
    cleanup_branch = cfg.cleanup_branch or DEFAULT_CLEANUP_BRANCH
    outcome = await run_batch(
        repo=repo,
        cleanup_branch=cleanup_branch,
        batch=batch,
        started_by_user_id=cfg.started_by_user_id,
    )
    log.info(
        "health_loop.batch_done",
        repo_id=repo_id,
        status=outcome.status,
        detail=outcome.detail[:200],
        fix_pr_url=outcome.fix_pr_url,
    )

    # Bookkeeping. Mark members addressed on a real verdict (merged / parked /
    # no-op) so they aren't re-picked. An "error" is an infra/transient failure,
    # NOT a verdict — leave those findings un-addressed so the next tick retries.
    if outcome.status == "merged":
        await mark_addressed(repo_id, outcome.finding_hashes)
        await record_outcome(repo_id, merged=1)
        if outcome.cleanup_pr_url:
            await set_cleanup_pr_url(repo_id, outcome.cleanup_pr_url)
    elif outcome.status == "parked":
        await mark_addressed(repo_id, outcome.finding_hashes)
        await record_outcome(repo_id, parked=1)
    elif outcome.status == "noop":
        await mark_addressed(repo_id, outcome.finding_hashes)
    await set_current_batch(repo_id, [])


async def _go_idle(repo_id: int) -> None:
    await set_state(repo_id, "idle")
    await set_current_batch(repo_id, [])


async def _load_repo(repo_id: int) -> Repo | None:
    async with async_session() as session:
        return await session.get(Repo, repo_id)
