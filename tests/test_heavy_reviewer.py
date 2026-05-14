"""Heavy per-item reviewer — ADR-015 §3 / Phase 7.

The "heavy" per-item reviewer replaces the readonly alignment-only
reviewer from ADR-013. For one backlog item it:

  1. Reads the item spec + builder diff (alignment).
  2. Greps the diff for stubs (no-defer enforcement).
  3. Boots the dev server + exercises ``affected_routes`` (smoke).
  4. Screenshots + UI-judges any UI route in scope.
  5. Writes ``.auto-agent/reviews/<item_id>.json`` via the
     ``submit-item-review`` skill.

Tests run end-to-end with the verify primitives + the skills-bridge
mocked so we exercise the orchestration without spinning up real
processes.
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle.trio import reviewer as heavy_reviewer
from agent.lifecycle.verify_primitives import (
    RouteResult,
    ServerHandle,
    UIResult,
)

if TYPE_CHECKING:
    from pathlib import Path

_ITEM_OK = {
    "id": "T1",
    "title": "Add /widgets endpoint",
    "description": "Adds a GET /widgets route returning a list of widgets.",
    "justification": "Slice owns the routing entry point so other items can register handlers.",
    "affected_routes": ["/widgets"],
    "affected_files_estimate": 2,
}


_DIFF_CLEAN = textwrap.dedent(
    """\
    diff --git a/api/routes.py b/api/routes.py
    --- a/api/routes.py
    +++ b/api/routes.py
    @@ -1,3 +1,7 @@
     from fastapi import APIRouter
     router = APIRouter()
    +
    +@router.get("/widgets")
    +async def list_widgets():
    +    return [{"id": 1}]
    """
)


_DIFF_WITH_STUB = textwrap.dedent(
    """\
    diff --git a/api/routes.py b/api/routes.py
    --- a/api/routes.py
    +++ b/api/routes.py
    @@ -1,3 +1,7 @@
     from fastapi import APIRouter
     router = APIRouter()
    +
    +@router.get("/widgets")
    +async def list_widgets():
    +    raise NotImplementedError("Phase 1 fills this in later")
    """
)


def _running_handle() -> ServerHandle:
    return ServerHandle(state="running", base_url="http://127.0.0.1:8000", port=8000)


@pytest.mark.asyncio
async def test_heavy_review_pass_writes_review_json(tmp_path: Path) -> None:
    """All checks pass → verdict=pass + writes reviews/<id>.json with schema_version."""

    workspace = tmp_path

    route_results = {"/widgets": RouteResult(ok=True, status=200, body="[{}]")}

    with (
        patch.object(heavy_reviewer, "_load_item_diff", AsyncMock(return_value=_DIFF_CLEAN)),
        patch.object(heavy_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
        patch.object(heavy_reviewer, "exercise_routes", AsyncMock(return_value=route_results)),
        patch.object(
            heavy_reviewer, "inspect_ui", AsyncMock(return_value=UIResult(ok=True, reason="ok"))
        ),
        patch.object(heavy_reviewer, "_run_alignment_agent", AsyncMock(return_value="aligned")),
    ):
        result = await heavy_reviewer.run_heavy_review(
            item=_ITEM_OK,
            workspace_root=str(workspace),
            base_sha="abc",
        )

    assert result.verdict == "pass"
    review_path = workspace / ".auto-agent" / "reviews" / "T1.json"
    assert review_path.exists()
    payload = json.loads(review_path.read_text())
    assert payload["schema_version"] == "1"
    assert payload["verdict"] == "pass"


@pytest.mark.asyncio
async def test_heavy_review_fails_on_stub_in_diff(tmp_path: Path) -> None:
    """grep_diff_for_stubs hit → verdict=fail with reason mentioning the pattern."""

    workspace = tmp_path

    with (
        patch.object(heavy_reviewer, "_load_item_diff", AsyncMock(return_value=_DIFF_WITH_STUB)),
        patch.object(heavy_reviewer, "boot_dev_server", AsyncMock()),
        patch.object(heavy_reviewer, "exercise_routes", AsyncMock()),
        patch.object(heavy_reviewer, "inspect_ui", AsyncMock()),
        patch.object(heavy_reviewer, "_run_alignment_agent", AsyncMock(return_value="aligned")),
    ):
        result = await heavy_reviewer.run_heavy_review(
            item=_ITEM_OK,
            workspace_root=str(workspace),
            base_sha="abc",
        )

    assert result.verdict == "fail"
    assert "NotImplementedError" in result.reason or "stub" in result.reason.lower()


@pytest.mark.asyncio
async def test_heavy_review_fails_on_500_route(tmp_path: Path) -> None:
    """Route returning 500 → verdict=fail with route info in reason."""

    workspace = tmp_path

    bad_route = {
        "/widgets": RouteResult(
            ok=False, status=500, body="NotImplementedError", reason="runtime_stub_shape"
        )
    }

    with (
        patch.object(heavy_reviewer, "_load_item_diff", AsyncMock(return_value=_DIFF_CLEAN)),
        patch.object(heavy_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
        patch.object(heavy_reviewer, "exercise_routes", AsyncMock(return_value=bad_route)),
        patch.object(heavy_reviewer, "inspect_ui", AsyncMock()),
        patch.object(heavy_reviewer, "_run_alignment_agent", AsyncMock(return_value="aligned")),
    ):
        result = await heavy_reviewer.run_heavy_review(
            item=_ITEM_OK,
            workspace_root=str(workspace),
            base_sha="abc",
        )

    assert result.verdict == "fail"
    assert "/widgets" in result.reason
    assert "500" in result.reason or "smoke" in result.reason.lower()


@pytest.mark.asyncio
async def test_heavy_review_fails_on_ui_inspect_fail(tmp_path: Path) -> None:
    """UI inspect FAIL on a UI route → verdict=fail."""

    workspace = tmp_path

    ui_item = dict(_ITEM_OK)
    ui_item["id"] = "T2"
    ui_item["affected_routes"] = ["/dashboard"]

    route_results = {"/dashboard": RouteResult(ok=True, status=200, body="<html>")}

    with (
        patch.object(heavy_reviewer, "_load_item_diff", AsyncMock(return_value=_DIFF_CLEAN)),
        patch.object(heavy_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
        patch.object(heavy_reviewer, "exercise_routes", AsyncMock(return_value=route_results)),
        patch.object(
            heavy_reviewer,
            "inspect_ui",
            AsyncMock(return_value=UIResult(ok=False, reason="dashboard shows blank state")),
        ),
        patch.object(heavy_reviewer, "_run_alignment_agent", AsyncMock(return_value="aligned")),
    ):
        result = await heavy_reviewer.run_heavy_review(
            item=ui_item,
            workspace_root=str(workspace),
            base_sha="abc",
        )

    assert result.verdict == "fail"
    assert "ui" in result.reason.lower() or "dashboard" in result.reason.lower()


@pytest.mark.asyncio
async def test_heavy_review_fails_on_alignment_mismatch(tmp_path: Path) -> None:
    """Alignment agent says the diff doesn't match the spec → verdict=fail."""

    workspace = tmp_path

    route_results = {"/widgets": RouteResult(ok=True, status=200, body="[]")}

    with (
        patch.object(heavy_reviewer, "_load_item_diff", AsyncMock(return_value=_DIFF_CLEAN)),
        patch.object(heavy_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
        patch.object(heavy_reviewer, "exercise_routes", AsyncMock(return_value=route_results)),
        patch.object(heavy_reviewer, "inspect_ui", AsyncMock(return_value=UIResult(ok=True))),
        patch.object(
            heavy_reviewer,
            "_run_alignment_agent",
            AsyncMock(return_value="FAIL: diff renames an unrelated module"),
        ),
    ):
        result = await heavy_reviewer.run_heavy_review(
            item=_ITEM_OK,
            workspace_root=str(workspace),
            base_sha="abc",
        )

    assert result.verdict == "fail"
    assert "align" in result.reason.lower() or "spec" in result.reason.lower()


@pytest.mark.asyncio
async def test_heavy_review_unions_architect_and_inferred_routes(tmp_path: Path) -> None:
    """Reviewer exercises union(item.affected_routes, infer_routes_from_diff(diff))."""

    workspace = tmp_path

    item = dict(_ITEM_OK)
    item["affected_routes"] = ["/declared"]

    # Diff adds @router.get("/discovered").
    diff = textwrap.dedent(
        """\
        diff --git a/api/r.py b/api/r.py
        --- a/api/r.py
        +++ b/api/r.py
        @@ -1,3 +1,5 @@
        +@router.get("/discovered")
        +async def hello(): return {"x":1}
        """
    )

    seen_routes: list[list[str]] = []

    async def fake_exercise(routes, *, handle):
        seen_routes.append(list(routes))
        return {r: RouteResult(ok=True, status=200, body="ok") for r in routes}

    with (
        patch.object(heavy_reviewer, "_load_item_diff", AsyncMock(return_value=diff)),
        patch.object(heavy_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
        patch.object(heavy_reviewer, "exercise_routes", side_effect=fake_exercise),
        patch.object(heavy_reviewer, "inspect_ui", AsyncMock(return_value=UIResult(ok=True))),
        patch.object(heavy_reviewer, "_run_alignment_agent", AsyncMock(return_value="aligned")),
    ):
        await heavy_reviewer.run_heavy_review(
            item=item,
            workspace_root=str(workspace),
            base_sha="abc",
        )

    assert seen_routes, "exercise_routes was not called"
    routes_used = set(seen_routes[0])
    assert "/declared" in routes_used
    assert "/discovered" in routes_used
