"""Per-repo graph workspace lock (ADR-016 §9).

The lock has three load-bearing properties Phase 2's analyser will rely on:

1. ``async with graph_workspace_lock(repo_id):`` serialises blocks for the
   same ``repo_id`` — concurrent refreshes never tread on each other's
   git tree.
2. Locks for different ``repo_id``s are independent — analysis of repo A
   never blocks analysis of repo B.
3. The lock is async-friendly — the underlying ``fcntl.flock`` call runs
   inside ``asyncio.to_thread`` so a held lock never stalls the event loop.

Also covered: ``graph_workspace_path`` is purely a function of the
configured root and the repo_id (no user-controlled segments), and the
``ensure_graph_workspace`` parent directory creation is idempotent.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from agent.graph_workspace import (
    GraphWorkspaceLockTimeout,
    ensure_graph_workspace_parent,
    graph_workspace_lock,
    graph_workspace_path,
)


def test_path_is_deterministic_per_repo(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_WORKSPACES_DIR", str(tmp_path))
    p1 = graph_workspace_path(repo_id=42)
    p2 = graph_workspace_path(repo_id=42)
    assert p1 == p2
    assert p1.endswith(os.path.join(str(tmp_path), "42"))


def test_paths_differ_per_repo(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_WORKSPACES_DIR", str(tmp_path))
    assert graph_workspace_path(repo_id=1) != graph_workspace_path(repo_id=2)


def test_ensure_parent_is_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_WORKSPACES_DIR", str(tmp_path / "graphs"))
    # Twice on purpose — must not raise.
    ensure_graph_workspace_parent(repo_id=7)
    ensure_graph_workspace_parent(repo_id=7)
    assert os.path.isdir(str(tmp_path / "graphs"))


@pytest.mark.asyncio
async def test_lock_serialises_same_repo(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_WORKSPACES_DIR", str(tmp_path))

    timeline: list[tuple[str, float]] = []
    start = time.monotonic()

    async def hold(name: str) -> None:
        async with graph_workspace_lock(repo_id=1):
            timeline.append((f"{name}:enter", time.monotonic() - start))
            # Long enough that overlap would be unambiguous if the lock
            # weren't doing its job.
            await asyncio.sleep(0.15)
            timeline.append((f"{name}:exit", time.monotonic() - start))

    await asyncio.gather(hold("a"), hold("b"))

    # Whoever entered second must have done so only after the first exited.
    events = {name: t for name, t in timeline}
    a_enter, a_exit = events["a:enter"], events["a:exit"]
    b_enter, b_exit = events["b:enter"], events["b:exit"]
    if a_enter < b_enter:
        first_exit, second_enter = a_exit, b_enter
    else:
        first_exit, second_enter = b_exit, a_enter
    assert second_enter >= first_exit - 0.01  # tiny scheduling slack


@pytest.mark.asyncio
async def test_lock_independent_across_repos(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_WORKSPACES_DIR", str(tmp_path))

    enter_a = asyncio.Event()
    release_a = asyncio.Event()

    async def hold_a() -> None:
        async with graph_workspace_lock(repo_id=1):
            enter_a.set()
            await release_a.wait()

    async def hold_b() -> None:
        # Wait for A to be holding its lock, then prove B can still enter.
        await enter_a.wait()
        async with graph_workspace_lock(repo_id=2):
            # B got in despite A still holding repo 1.
            release_a.set()

    await asyncio.wait_for(asyncio.gather(hold_a(), hold_b()), timeout=1.5)


@pytest.mark.asyncio
async def test_lock_timeout_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_WORKSPACES_DIR", str(tmp_path))

    enter = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with graph_workspace_lock(repo_id=99):
            enter.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    try:
        await enter.wait()
        with pytest.raises(GraphWorkspaceLockTimeout):
            async with graph_workspace_lock(repo_id=99, timeout=0.1):
                pytest.fail("should not have acquired")
    finally:
        release.set()
        await holder


@pytest.mark.asyncio
async def test_lock_releases_on_exception(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GRAPH_WORKSPACES_DIR", str(tmp_path))

    class Boom(RuntimeError):  # noqa: N818 — local throwaway test exception
        pass

    with pytest.raises(Boom):
        async with graph_workspace_lock(repo_id=5):
            raise Boom

    # The next attempt must succeed quickly — the lock file is not stuck.
    async with graph_workspace_lock(repo_id=5, timeout=0.5):
        pass
