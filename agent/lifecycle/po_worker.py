"""PO analysis worker — runs Product Owner analyses sequentially.

The worker drains a single asyncio.Queue: when a ``po.analyze`` event lands,
``handle`` enqueues the repo_id (and emits ``po.analysis_queued`` if there
was already a backlog so the user gets a heads-up). ``start`` boots the
background worker task — call it once at process startup.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from shared.events import Event, po_analysis_queued, publish
from shared.logging import setup_logging

log = setup_logging("agent.lifecycle.po_worker")


_po_queue: asyncio.Queue[int] = asyncio.Queue()


async def _po_worker() -> None:
    """Background worker — runs PO analyses sequentially."""
    from sqlalchemy import select as _select

    from agent.po_analyzer import handle_po_analysis as _handle_po
    from shared.database import async_session as _async_session
    from shared.models import FreeformConfig as _FC

    log.info("PO analysis worker started")
    while True:
        repo_id = await _po_queue.get()
        try:
            async with _async_session() as _session:
                _result = await _session.execute(_select(_FC).where(_FC.repo_id == repo_id))
                _config = _result.scalar_one_or_none()
                if _config:
                    await _handle_po(_session, _config)
                    _config.last_analysis_at = datetime.now(UTC)
                    await _session.commit()
        except Exception:
            log.exception(f"PO analysis worker error for repo_id={repo_id}")
        finally:
            _po_queue.task_done()


def start() -> asyncio.Task:
    """Boot the background PO worker. Call once at process startup."""
    return asyncio.create_task(_po_worker())


async def handle(event: Event) -> None:
    """EventBus entry — enqueue a repo for PO analysis.

    Emits ``po.analysis_queued`` if there was already work in the queue, so
    the user knows their analysis is backlogged rather than running now.
    """
    if not event.payload:
        return
    repo_id = event.payload.get("repo_id")
    repo_name = event.payload.get("repo_name", "")
    if not repo_id:
        return
    queued = _po_queue.qsize() > 0
    await _po_queue.put(repo_id)
    if queued:
        await publish(
            po_analysis_queued(repo_name=repo_name, position=_po_queue.qsize())
        )
