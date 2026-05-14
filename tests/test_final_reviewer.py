"""Final reviewer — ADR-015 §4 / Phase 7.

After every per-item review passes for a complex_large task, the
orchestrator runs ONE final reviewer over the integrated diff:

  - Context: ``.auto-agent/design.md``, all per-item ``reviews/*.json``,
    the integrated diff, the original grill output.
  - Action: smoke + UI across the UNION of all ``affected_routes``.
  - Output: ``.auto-agent/final_review.json`` via ``submit-final-review``.

Verdict is ``"passed"`` or ``"gaps_found"``. Final reviewer is fresh
each gap-fix round — no persisted session.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle.trio import final_reviewer

if TYPE_CHECKING:
    from pathlib import Path
from agent.lifecycle.verify_primitives import (
    RouteResult,
    ServerHandle,
    UIResult,
)


def _seed_workspace(
    workspace: Path,
    *,
    design: str = "# design\n\nbuild X.",
    items: list[dict] | None = None,
    reviews: dict[str, dict] | None = None,
) -> None:
    """Create .auto-agent/design.md, .auto-agent/backlog.json, reviews/."""

    auto = workspace / ".auto-agent"
    auto.mkdir(exist_ok=True)
    (auto / "design.md").write_text(design)

    if items is not None:
        (auto / "backlog.json").write_text(json.dumps({"schema_version": "1", "items": items}))

    reviews_dir = auto / "reviews"
    reviews_dir.mkdir(exist_ok=True)
    for item_id, review in (reviews or {}).items():
        (reviews_dir / f"{item_id}.json").write_text(json.dumps(review))


def _running_handle() -> ServerHandle:
    return ServerHandle(state="running", base_url="http://127.0.0.1:9000", port=9000)


@pytest.mark.asyncio
async def test_final_review_pass_writes_final_review_json(tmp_path: Path) -> None:
    """Smoke + UI green across the union of routes → final_review.json verdict=passed."""

    workspace = tmp_path
    items = [
        {
            "id": "T1",
            "title": "A",
            "description": "x",
            "affected_routes": ["/api/a"],
        },
        {
            "id": "T2",
            "title": "B",
            "description": "y",
            "affected_routes": ["/api/b"],
        },
    ]
    reviews = {
        "T1": {"schema_version": "1", "verdict": "pass"},
        "T2": {"schema_version": "1", "verdict": "pass"},
    }
    _seed_workspace(workspace, items=items, reviews=reviews)

    route_results = {
        "/api/a": RouteResult(ok=True, status=200, body="{}"),
        "/api/b": RouteResult(ok=True, status=200, body="{}"),
    }

    with (
        patch.object(final_reviewer, "_load_integrated_diff", AsyncMock(return_value="")),
        patch.object(final_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
        patch.object(final_reviewer, "exercise_routes", AsyncMock(return_value=route_results)),
        patch.object(final_reviewer, "inspect_ui", AsyncMock(return_value=UIResult(ok=True))),
        patch.object(
            final_reviewer,
            "_run_final_review_agent",
            AsyncMock(return_value="all good"),
        ),
    ):
        result = await final_reviewer.run_final_review(
            workspace_root=str(workspace),
            parent_task_id=1,
            grill_output="user wants X",
            base_branch="main",
        )

    assert result.verdict == "passed"
    final = workspace / ".auto-agent" / "final_review.json"
    assert final.exists()
    payload = json.loads(final.read_text())
    assert payload["schema_version"] == "1"
    assert payload["verdict"] == "passed"


@pytest.mark.asyncio
async def test_final_review_gaps_found_writes_gap_list(tmp_path: Path) -> None:
    """At least one route fails → verdict=gaps_found with gap description + routes."""

    workspace = tmp_path
    items = [
        {"id": "T1", "title": "A", "description": "x", "affected_routes": ["/api/a"]},
    ]
    _seed_workspace(
        workspace,
        items=items,
        reviews={"T1": {"schema_version": "1", "verdict": "pass"}},
    )

    bad_routes = {"/api/a": RouteResult(ok=False, status=500, body="boom", reason="server_5xx")}

    with (
        patch.object(final_reviewer, "_load_integrated_diff", AsyncMock(return_value="")),
        patch.object(final_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
        patch.object(final_reviewer, "exercise_routes", AsyncMock(return_value=bad_routes)),
        patch.object(final_reviewer, "inspect_ui", AsyncMock(return_value=UIResult(ok=True))),
        patch.object(
            final_reviewer,
            "_run_final_review_agent",
            AsyncMock(return_value="problem on /api/a"),
        ),
    ):
        result = await final_reviewer.run_final_review(
            workspace_root=str(workspace),
            parent_task_id=1,
            grill_output="",
            base_branch="main",
        )

    assert result.verdict == "gaps_found"
    assert result.gaps, "expected at least one gap"
    affected = []
    for g in result.gaps:
        affected.extend(g.get("affected_routes") or [])
    assert "/api/a" in affected


@pytest.mark.asyncio
async def test_final_review_unions_affected_routes(tmp_path: Path) -> None:
    """Exercise_routes must be called with union of all items' affected_routes."""

    workspace = tmp_path
    items = [
        {"id": "T1", "title": "A", "description": "x", "affected_routes": ["/a"]},
        {"id": "T2", "title": "B", "description": "y", "affected_routes": ["/b", "/a"]},
        {"id": "T3", "title": "C", "description": "z", "affected_routes": ["/c"]},
    ]
    _seed_workspace(
        workspace,
        items=items,
        reviews={
            "T1": {"schema_version": "1", "verdict": "pass"},
            "T2": {"schema_version": "1", "verdict": "pass"},
            "T3": {"schema_version": "1", "verdict": "pass"},
        },
    )

    seen: list[list[str]] = []

    async def fake_exercise(routes, *, handle):
        seen.append(list(routes))
        return {r: RouteResult(ok=True, status=200, body="ok") for r in routes}

    with (
        patch.object(final_reviewer, "_load_integrated_diff", AsyncMock(return_value="")),
        patch.object(final_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
        patch.object(final_reviewer, "exercise_routes", side_effect=fake_exercise),
        patch.object(final_reviewer, "inspect_ui", AsyncMock(return_value=UIResult(ok=True))),
        patch.object(final_reviewer, "_run_final_review_agent", AsyncMock(return_value="")),
    ):
        await final_reviewer.run_final_review(
            workspace_root=str(workspace),
            parent_task_id=1,
            grill_output="",
            base_branch="main",
        )

    assert seen, "exercise_routes never called"
    routes_used = set(seen[0])
    assert routes_used == {"/a", "/b", "/c"}


@pytest.mark.asyncio
async def test_final_review_is_fresh_each_round(tmp_path: Path) -> None:
    """The final reviewer agent must be created fresh per call — no persisted Session.

    Asserts that ``create_agent`` is called without a ``session=`` kwarg
    (or with ``session=None``) per ADR-015 §4 — the design doc, gap list
    and previous attempt summary are explicit in the prompt, not in a
    resumed session.
    """

    workspace = tmp_path
    _seed_workspace(workspace, items=[], reviews={})

    captured_kwargs: list[dict] = []

    def fake_create_agent(*args, **kwargs):
        captured_kwargs.append(kwargs)

        class _Agent:
            async def run(self, *_a, **_kw):
                # Pretend the agent invoked submit-final-review.
                (workspace / ".auto-agent").mkdir(exist_ok=True)
                (workspace / ".auto-agent" / "final_review.json").write_text(
                    json.dumps({"schema_version": "1", "verdict": "passed", "gaps": []})
                )

                class R:
                    output = "wrote final_review.json"

                return R()

        return _Agent()

    with (
        patch.object(final_reviewer, "_load_integrated_diff", AsyncMock(return_value="")),
        patch.object(final_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
        patch.object(final_reviewer, "exercise_routes", AsyncMock(return_value={})),
        patch.object(final_reviewer, "inspect_ui", AsyncMock(return_value=UIResult(ok=True))),
        patch.object(final_reviewer, "create_agent", fake_create_agent),
    ):
        await final_reviewer.run_final_review(
            workspace_root=str(workspace),
            parent_task_id=99,
            grill_output="",
            base_branch="main",
            previous_gaps=[{"description": "old", "affected_routes": []}],
            previous_attempt_summary="round 1 attempt",
        )

    assert captured_kwargs, "create_agent was not called"
    for kw in captured_kwargs:
        # session must not be set (or must be None) — fresh agent each round.
        assert kw.get("session") is None
