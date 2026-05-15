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
from agent.lifecycle.trio.smoke_agent import SmokeAgentResult
from agent.lifecycle.verify_primitives import (
    ServerHandle,
    UIResult,
)


def _pass_smoke() -> SmokeAgentResult:
    return SmokeAgentResult(verdict="pass", summary="ran pytest -q; all green")


def _fail_smoke(reason: str) -> SmokeAgentResult:
    return SmokeAgentResult(verdict="fail", summary=reason, failures=[reason])


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

    with (
        patch.object(heavy_reviewer, "_load_item_diff", AsyncMock(return_value=_DIFF_CLEAN)),
        patch.object(heavy_reviewer, "run_smoke_agent", AsyncMock(return_value=_pass_smoke())),
        patch.object(heavy_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
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
        patch.object(heavy_reviewer, "run_smoke_agent", AsyncMock(return_value=_pass_smoke())),
        patch.object(heavy_reviewer, "boot_dev_server", AsyncMock()),
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
async def test_heavy_review_fails_when_smoke_agent_reports_failure(tmp_path: Path) -> None:
    """Phase 7.8: smoke is owned by the dedicated smoke agent.

    A failing smoke verdict (broken boot, 5xx route, failing tests,
    failed typecheck — the agent decides what broke) short-circuits the
    per-item review with ``verdict="fail"`` and the agent's first
    failure surfaced in the reason. There is no longer a separate
    "route returned 500" branch in the reviewer.
    """

    workspace = tmp_path
    smoke_fail = _fail_smoke("/widgets returned 500 with NotImplementedError traceback")

    with (
        patch.object(heavy_reviewer, "_load_item_diff", AsyncMock(return_value=_DIFF_CLEAN)),
        patch.object(heavy_reviewer, "run_smoke_agent", AsyncMock(return_value=smoke_fail)),
        patch.object(heavy_reviewer, "boot_dev_server", AsyncMock()),
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

    with (
        patch.object(heavy_reviewer, "_load_item_diff", AsyncMock(return_value=_DIFF_CLEAN)),
        patch.object(heavy_reviewer, "run_smoke_agent", AsyncMock(return_value=_pass_smoke())),
        patch.object(heavy_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
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

    with (
        patch.object(heavy_reviewer, "_load_item_diff", AsyncMock(return_value=_DIFF_CLEAN)),
        patch.object(heavy_reviewer, "run_smoke_agent", AsyncMock(return_value=_pass_smoke())),
        patch.object(heavy_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
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
async def test_heavy_review_inspects_ui_routes_from_declared_and_inferred(tmp_path: Path) -> None:
    """Phase 7.8: the UI-inspection layer (on top of smoke) still sweeps
    the union of architect-declared and diff-inferred routes.

    Smoke proves the code runs; this layer is the visual-correctness
    pass for UI routes that returned 2xx. Non-UI API routes don't
    invoke ``inspect_ui`` — they're the smoke agent's job exclusively.
    """

    workspace = tmp_path

    item = dict(_ITEM_OK)
    # Use UI routes so the inspect_ui sweep fires.
    item["affected_routes"] = ["/dashboard"]

    # Diff adds Next.js page route at /reports.
    diff = textwrap.dedent(
        """\
        diff --git a/web-next/app/(app)/reports/page.tsx b/web-next/app/(app)/reports/page.tsx
        --- /dev/null
        +++ b/web-next/app/(app)/reports/page.tsx
        @@ -0,0 +1,3 @@
        +export default function Page() {
        +  return <h1>Reports</h1>;
        +}
        """
    )

    inspected_routes: list[str] = []

    async def fake_inspect(*, route, intent, base_url):
        inspected_routes.append(route)
        return UIResult(ok=True)

    with (
        patch.object(heavy_reviewer, "_load_item_diff", AsyncMock(return_value=diff)),
        patch.object(heavy_reviewer, "run_smoke_agent", AsyncMock(return_value=_pass_smoke())),
        patch.object(heavy_reviewer, "boot_dev_server", AsyncMock(return_value=_running_handle())),
        patch.object(heavy_reviewer, "inspect_ui", side_effect=fake_inspect),
        patch.object(heavy_reviewer, "_run_alignment_agent", AsyncMock(return_value="aligned")),
    ):
        result = await heavy_reviewer.run_heavy_review(
            item=item,
            workspace_root=str(workspace),
            base_sha="abc",
        )

    assert result.verdict == "pass"
    assert "/dashboard" in inspected_routes
    assert "/reports" in inspected_routes
