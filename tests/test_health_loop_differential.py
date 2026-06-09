"""Phase 2 — route-response differential verifier."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.health_loop import differential
from agent.health_loop.differential import (
    RouteDiff,
    compare_route,
    diff_results,
    differential_verify,
)
from agent.lifecycle.verify_primitives import RouteResult, ServerHandle


def test_identical_responses_have_no_diff():
    base = RouteResult(ok=True, status=200, body='{"a": 1, "b": 2}')
    branch = RouteResult(ok=True, status=200, body='{"b": 2, "a": 1}')  # key order differs
    # JSON-aware comparison ⇒ key order is not a difference.
    assert compare_route("/x", base, branch) is None


def test_status_change_is_a_diff():
    base = RouteResult(ok=True, status=200, body="ok")
    branch = RouteResult(ok=False, status=500, body="ok")
    d = compare_route("/x", base, branch)
    assert isinstance(d, RouteDiff)
    assert d.route == "/x"
    assert "200" in d.detail and "500" in d.detail


def test_body_value_change_is_a_diff():
    base = RouteResult(ok=True, status=200, body='{"total": 10}')
    branch = RouteResult(ok=True, status=200, body='{"total": 11}')
    d = compare_route("/x", base, branch)
    assert d is not None
    assert "body" in d.detail.lower()


def test_non_json_body_compared_as_stripped_text():
    base = RouteResult(ok=True, status=200, body="  hello  ")
    branch = RouteResult(ok=True, status=200, body="hello")
    assert compare_route("/x", base, branch) is None  # whitespace-only ⇒ no diff


def test_diff_results_flags_changed_route_only():
    base = {
        "/a": RouteResult(ok=True, status=200, body="x"),
        "/b": RouteResult(ok=True, status=200, body="y"),
    }
    branch = {
        "/a": RouteResult(ok=True, status=200, body="x"),  # unchanged
        "/b": RouteResult(ok=True, status=500, body="y"),  # changed
    }
    diffs = diff_results(base, branch)
    assert [d.route for d in diffs] == ["/b"]


def test_diff_results_flags_route_missing_on_one_side():
    base = {"/a": RouteResult(ok=True, status=200, body="x")}
    branch = {}  # /a disappeared from the branch
    diffs = diff_results(base, branch)
    assert len(diffs) == 1
    assert diffs[0].route == "/a"
    assert "missing" in diffs[0].detail.lower()


def test_diff_results_empty_when_identical():
    base = {"/a": RouteResult(ok=True, status=200, body="x")}
    branch = {"/a": RouteResult(ok=True, status=200, body="x")}
    assert diff_results(base, branch) == []


def _running(url="http://127.0.0.1:9000"):
    return ServerHandle(state="running", base_url=url, port=9000)


@pytest.mark.asyncio
async def test_verify_no_regression_when_responses_match():
    same = {"/a": RouteResult(ok=True, status=200, body="x")}
    with (
        patch.object(differential, "boot_dev_server", AsyncMock(return_value=_running())),
        patch.object(differential, "exercise_routes", AsyncMock(return_value=same)),
        patch.object(ServerHandle, "teardown", AsyncMock()),
    ):
        result = await differential_verify(
            base_workspace="/tmp/base",
            branch_workspace="/tmp/branch",
            routes=["/a"],
        )
    assert result.regressed is False
    assert result.diffs == []


@pytest.mark.asyncio
async def test_verify_regression_when_a_route_changes():
    base = {"/a": RouteResult(ok=True, status=200, body="x")}
    branch = {"/a": RouteResult(ok=True, status=500, body="x")}
    with (
        patch.object(differential, "boot_dev_server", AsyncMock(return_value=_running())),
        patch.object(differential, "exercise_routes", AsyncMock(side_effect=[base, branch])),
        patch.object(ServerHandle, "teardown", AsyncMock()),
    ):
        result = await differential_verify(
            base_workspace="/tmp/base",
            branch_workspace="/tmp/branch",
            routes=["/a"],
        )
    assert result.regressed is True
    assert result.diffs[0].route == "/a"


@pytest.mark.asyncio
async def test_verify_regression_when_boot_state_diverges():
    # base boots, branch fails to boot ⇒ the fix broke startup ⇒ regression.
    handles = [_running(), ServerHandle(state="failed", failure_reason="boom")]
    with (
        patch.object(differential, "boot_dev_server", AsyncMock(side_effect=handles)),
        patch.object(differential, "exercise_routes", AsyncMock(return_value={})),
        patch.object(ServerHandle, "teardown", AsyncMock()),
    ):
        result = await differential_verify(
            base_workspace="/tmp/base",
            branch_workspace="/tmp/branch",
            routes=["/a"],
        )
    assert result.regressed is True
    assert "boot" in result.note.lower()


@pytest.mark.asyncio
async def test_verify_no_regression_when_neither_boots():
    disabled = ServerHandle.disabled()
    with (
        patch.object(differential, "boot_dev_server", AsyncMock(return_value=disabled)),
        patch.object(differential, "exercise_routes", AsyncMock(return_value={})),
        patch.object(ServerHandle, "teardown", AsyncMock()),
    ):
        result = await differential_verify(
            base_workspace="/tmp/base",
            branch_workspace="/tmp/branch",
            routes=["/a"],
        )
    # No bootable surface to compare ⇒ no observable regression (degenerate).
    assert result.regressed is False
    assert "boot" in result.note.lower()


@pytest.mark.asyncio
async def test_verify_tears_down_both_servers():
    same = {"/a": RouteResult(ok=True, status=200, body="x")}
    teardown = AsyncMock()
    with (
        patch.object(differential, "boot_dev_server", AsyncMock(return_value=_running())),
        patch.object(differential, "exercise_routes", AsyncMock(return_value=same)),
        patch.object(ServerHandle, "teardown", teardown),
    ):
        await differential_verify(
            base_workspace="/tmp/base",
            branch_workspace="/tmp/branch",
            routes=["/a"],
        )
    # Booted twice (base + branch) ⇒ torn down twice.
    assert teardown.await_count == 2
