"""Per-repo workspaces for the code-graph analyser (ADR-016 §9).

The analyser (Phase 2) needs a checkout that is independent from the task
workspaces under ``WORKSPACES_DIR`` because:

* task workspaces churn between feature branches every time a task runs;
* an in-flight analyse on a repo must not collide with an in-flight
  ``git checkout`` for a task on the same repo.

This module owns:

* ``graph_workspace_path(repo_id)`` — deterministic per-repo directory.
* ``ensure_graph_workspace_parent(repo_id)`` — create the parent directory.
* ``graph_workspace_lock(repo_id, timeout=...)`` — async context manager
  backed by an ``fcntl.flock`` exclusive lock on a per-repo lockfile.
  Cross-process safe, awaitable, and releases the lock on exception.

Phase 1 ships only the path resolution + lock primitive. Cloning/refreshing
the workspace itself lands in Phase 2 once the analyser is wired in — by
which time these primitives already underpin the contention story.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = structlog.get_logger()


class GraphWorkspaceLockTimeout(TimeoutError):  # noqa: N818
    """Raised when ``graph_workspace_lock`` cannot acquire within ``timeout``.

    Subclasses ``TimeoutError`` so it slots into existing
    ``except TimeoutError`` blocks; the explicit ``Timeout`` suffix is
    the standard convention for the stdlib (``socket.timeout``,
    ``asyncio.TimeoutError``) and reads naturally in logs.
    """


def _graph_workspaces_dir() -> str:
    """Resolve the root of the graph workspaces tree.

    Reads the env var lazily so tests can ``monkeypatch.setenv`` between
    runs. Falls back to ``shared.config.settings.graph_workspaces_dir``
    so production picks up its default from the Settings model.
    """
    env = os.environ.get("GRAPH_WORKSPACES_DIR")
    if env:
        return env
    # Late import — settings depends on pydantic_settings which is fine,
    # but importing it at module-load time pulls in the orchestrator's
    # whole Settings model and complicates test isolation.
    from shared.config import settings

    return settings.graph_workspaces_dir


def graph_workspace_path(*, repo_id: int) -> str:
    """Return the on-disk workspace path for ``repo_id``.

    Pure function — no ``mkdir``, no IO. Callers that need the directory
    to exist call ``ensure_graph_workspace_parent`` first (or, in Phase 2,
    let ``clone`` create it).
    """
    return os.path.join(_graph_workspaces_dir(), str(repo_id))


def ensure_graph_workspace_parent(*, repo_id: int) -> str:
    """Create the parent directory for the repo's workspace.

    Returns the resolved workspace path. Idempotent — safe to call
    repeatedly. Does NOT create the repo's own subdirectory; ``git clone``
    must do that (clone refuses to write into a pre-existing directory).
    """
    workspace = graph_workspace_path(repo_id=repo_id)
    os.makedirs(os.path.dirname(workspace), exist_ok=True)
    return workspace


def _lock_file_path(*, repo_id: int) -> str:
    """Lockfile path for a repo. Lives alongside the workspace tree so the
    same volume + filesystem semantics apply (matters for NFS in prod)."""
    root = _graph_workspaces_dir()
    return os.path.join(root, f".lock-{repo_id}")


def _try_acquire(fd: int) -> bool:
    """Non-blocking ``flock`` attempt. Returns True on acquisition."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


@contextlib.asynccontextmanager
async def graph_workspace_lock(
    *,
    repo_id: int,
    timeout: float = 30.0,
    poll_interval: float = 0.05,
) -> AsyncIterator[None]:
    """Acquire a per-repo exclusive lock for the graph workspace.

    The lock is held for the lifetime of the ``async with`` block and
    released when the block exits (including via exception). Concurrent
    callers for the same ``repo_id`` are serialised; callers for different
    ``repo_id``s run in parallel.

    Uses ``fcntl.flock(LOCK_EX | LOCK_NB)`` in a polling loop wrapped in
    ``asyncio.to_thread`` so the event loop never blocks on a held lock.

    Raises ``GraphWorkspaceLockTimeout`` if ``timeout`` seconds elapse
    without acquisition. The default of 30 s is comfortably longer than
    any single analyser step Phase 2 will perform.
    """
    root = _graph_workspaces_dir()
    os.makedirs(root, exist_ok=True)
    path = _lock_file_path(repo_id=repo_id)

    # Open writeable so a stale empty file from a prior crash is fine.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            acquired = await asyncio.to_thread(_try_acquire, fd)
            if acquired:
                break
            if loop.time() >= deadline:
                raise GraphWorkspaceLockTimeout(
                    f"timeout acquiring graph workspace lock for repo_id={repo_id}"
                )
            await asyncio.sleep(poll_interval)

        log.debug("graph_workspace_lock_acquired", repo_id=repo_id, path=path)
        try:
            yield
        finally:
            # Best-effort release; closing the fd would also drop the lock
            # but doing it explicitly here surfaces unexpected errors.
            try:
                await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_UN)
            except OSError as e:
                log.warning(
                    "graph_workspace_lock_release_failed",
                    repo_id=repo_id,
                    error=str(e),
                )
    finally:
        os.close(fd)
