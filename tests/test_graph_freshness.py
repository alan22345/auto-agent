"""Tests for orchestrator/graph_freshness.py — debounced refresh-on-change (ADR-024).

Merge/push triggers call ``request_graph_refresh_soon`` fire-and-forget;
after a quiet period one ``repo.graph_requested`` event is published —
bursts collapse, changes to non-analysed branches are ignored, repos
without a completed graph are ignored, and failures never propagate to
the trigger (a graph refresh must never break a merge path).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator import graph_freshness


def _patch_analysis_branch(value: str | None):
    return patch.object(
        graph_freshness,
        "_analysis_branch_for",
        AsyncMock(return_value=value),
    )


def _patch_publish() -> AsyncMock:
    return patch.object(graph_freshness, "publish", AsyncMock())


@pytest.mark.asyncio
async def test_burst_of_changes_collapses_to_one_refresh() -> None:
    with _patch_analysis_branch("main"), _patch_publish() as publish:
        for _ in range(3):
            graph_freshness.request_graph_refresh_soon(7, delay_seconds=0.01)
        await asyncio.sleep(0.1)

    assert publish.await_count == 1
    event = publish.await_args.args[0]
    assert event.payload["repo_id"] == 7
    assert event.payload["request_id"]


@pytest.mark.asyncio
async def test_change_on_other_branch_is_ignored() -> None:
    with _patch_analysis_branch("main"), _patch_publish() as publish:
        graph_freshness.request_graph_refresh_soon(7, branch="feature/x", delay_seconds=0.01)
        await asyncio.sleep(0.05)

    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_on_the_analysis_branch_fires() -> None:
    with _patch_analysis_branch("main"), _patch_publish() as publish:
        graph_freshness.request_graph_refresh_soon(7, branch="main", delay_seconds=0.01)
        await asyncio.sleep(0.05)

    assert publish.await_count == 1


@pytest.mark.asyncio
async def test_repo_without_completed_graph_is_ignored() -> None:
    with _patch_analysis_branch(None), _patch_publish() as publish:
        graph_freshness.request_graph_refresh_soon(7, delay_seconds=0.01)
        await asyncio.sleep(0.05)

    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_failures_never_propagate() -> None:
    boom = AsyncMock(side_effect=RuntimeError("db down"))
    with patch.object(graph_freshness, "_analysis_branch_for", boom), _patch_publish() as publish:
        graph_freshness.request_graph_refresh_soon(7, delay_seconds=0.01)
        await asyncio.sleep(0.05)

    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_distinct_repos_debounce_independently() -> None:
    with _patch_analysis_branch("main"), _patch_publish() as publish:
        graph_freshness.request_graph_refresh_soon(7, delay_seconds=0.01)
        graph_freshness.request_graph_refresh_soon(8, delay_seconds=0.01)
        await asyncio.sleep(0.1)

    assert publish.await_count == 2
    repo_ids = {call.args[0].payload["repo_id"] for call in publish.await_args_list}
    assert repo_ids == {7, 8}
