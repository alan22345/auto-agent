"""Complex-flow verify wiring — ADR-015 §11 + §5 Phase 5.

After coding completes (but before the PR is opened), the complex flow
runs the shared verify primitives end-to-end against the working tree's
diff:

1. ``grep_diff_for_stubs(diff)`` — block on no-defer violations.
2. ``boot_dev_server`` + ``exercise_routes(routes)`` against routes
   inferred from the diff (same heuristic as Phase 4's PR-reviewer).
3. ``inspect_ui`` for any UI-flavoured route in the inferred set.

Results land in ``.auto-agent/smoke_result.json``. On a violation the
task goes back to CODING (1 retry). A second failure escalates to
BLOCKED.

The orchestrator entry point is
``agent.lifecycle.verify.run_verify_primitives_for_task``; the higher
level ``handle_verify`` delegates to it for the complex flow.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.lifecycle import verify
from agent.lifecycle.verify_primitives import (
    RouteResult,
    ServerHandle,
    StubResult,
    UIResult,
    Violation,
)
from agent.lifecycle.workspace_paths import SMOKE_RESULT_PATH

if TYPE_CHECKING:
    from pathlib import Path

_DIFF_OK = """\
diff --git a/api/routes.py b/api/routes.py
--- a/api/routes.py
+++ b/api/routes.py
@@ -1,3 +1,8 @@
 from fastapi import APIRouter
 router = APIRouter()
+
+@router.get("/widgets")
+async def list_widgets():
+    return [{"id": 1, "name": "a"}]
"""


_DIFF_WITH_STUB = """\
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


# ---------------------------------------------------------------------------
# All four primitives run, in the right order, and smoke_result.json is
# written.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_verify_primitives_invokes_all_four_in_order(tmp_path: Path) -> None:
    """``run_verify_primitives_for_task`` calls grep, boot, exercise, inspect
    in that order against the diff, then writes smoke_result.json."""

    call_log: list[str] = []

    handle = ServerHandle(state="running", base_url="http://127.0.0.1:8080", port=8080)

    async def fake_boot(**_kw):
        call_log.append("boot")
        return handle

    async def fake_exercise(routes, *, handle):
        call_log.append("exercise")
        return {r: RouteResult(ok=True, status=200, body="[]") for r in routes}

    async def fake_inspect(*, route, intent, base_url):
        call_log.append(f"inspect:{route}")
        return UIResult(ok=True, reason="looks correct")

    def fake_grep(diff: str) -> StubResult:
        call_log.append("grep")
        return StubResult(violations=[])

    async def fake_load_diff(workspace_root: str, *, base_branch: str = "main") -> str:
        return _DIFF_OK

    task = MagicMock()
    task.id = 100
    task.description = "List widgets"
    task.title = "Widgets endpoint"
    task.base_branch = "main"

    with (
        patch.object(verify, "_load_diff", AsyncMock(side_effect=fake_load_diff)),
        patch.object(verify, "grep_diff_for_stubs", fake_grep),
        patch.object(verify, "boot_dev_server", AsyncMock(side_effect=fake_boot)),
        patch.object(verify, "exercise_routes", AsyncMock(side_effect=fake_exercise)),
        patch.object(verify, "inspect_ui", AsyncMock(side_effect=fake_inspect)),
        patch.object(ServerHandle, "teardown", AsyncMock()),
    ):
        result = await verify.run_verify_primitives_for_task(
            task=task,
            workspace_root=str(tmp_path),
        )

    assert result.ok is True

    # Order: grep first, then boot, then exercise, then inspect.
    assert call_log[0] == "grep"
    assert call_log[1] == "boot"
    assert call_log[2] == "exercise"
    # The Widgets endpoint is API (/widgets has no API prefix in the diff
    # but it starts with /widgets, no /api/ prefix). The pr_reviewer
    # heuristic flags routes without /api/ or /v1/ as UI; reuse that here
    # so the inspect step fires.
    assert any(c.startswith("inspect:") for c in call_log)

    smoke_file = tmp_path / SMOKE_RESULT_PATH
    assert smoke_file.is_file()
    payload = json.loads(smoke_file.read_text())
    assert payload["schema_version"] == "1"
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# Stub violation triggers retry once (back to CODING); twice → BLOCKED.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_violation_first_attempt_transitions_back_to_coding(
    tmp_path: Path,
) -> None:
    """A stub violation on attempt 1 ⇒ transition to CODING for retry."""

    call_log: list[str] = []

    async def fake_load_diff(workspace_root: str, *, base_branch: str = "main") -> str:
        return _DIFF_WITH_STUB

    def fake_grep(diff: str) -> StubResult:
        call_log.append("grep")
        return StubResult(
            violations=[
                Violation(
                    file="api/routes.py",
                    line=5,
                    pattern="raise NotImplementedError",
                    snippet="    raise NotImplementedError(...)",
                )
            ]
        )

    task = MagicMock()
    task.id = 200
    task.title = "T"
    task.description = "D"
    task.base_branch = "main"
    # First attempt — no prior smoke failures.
    transition_mock = AsyncMock()

    with (
        patch.object(verify, "_load_diff", AsyncMock(side_effect=fake_load_diff)),
        patch.object(verify, "grep_diff_for_stubs", fake_grep),
        patch.object(verify, "transition_task", transition_mock),
    ):
        result = await verify.run_verify_primitives_for_task(
            task=task,
            workspace_root=str(tmp_path),
            attempt=1,
        )

    assert result.ok is False
    # The first attempt routes back to CODING.
    transition_mock.assert_awaited()
    last_call = transition_mock.call_args_list[-1]
    args, _ = last_call
    assert args[0] == 200
    assert args[1] == "coding"


@pytest.mark.asyncio
async def test_stub_violation_second_attempt_transitions_to_blocked(
    tmp_path: Path,
) -> None:
    """A stub violation on attempt 2 ⇒ BLOCKED (1-retry budget exhausted)."""

    async def fake_load_diff(workspace_root: str, *, base_branch: str = "main") -> str:
        return _DIFF_WITH_STUB

    def fake_grep(diff: str) -> StubResult:
        return StubResult(
            violations=[
                Violation(
                    file="api/routes.py",
                    line=5,
                    pattern="raise NotImplementedError",
                    snippet="    raise NotImplementedError(...)",
                )
            ]
        )

    task = MagicMock()
    task.id = 201
    task.title = "T"
    task.description = "D"
    task.base_branch = "main"
    transition_mock = AsyncMock()

    with (
        patch.object(verify, "_load_diff", AsyncMock(side_effect=fake_load_diff)),
        patch.object(verify, "grep_diff_for_stubs", fake_grep),
        patch.object(verify, "transition_task", transition_mock),
    ):
        result = await verify.run_verify_primitives_for_task(
            task=task,
            workspace_root=str(tmp_path),
            attempt=2,
        )

    assert result.ok is False
    transition_mock.assert_awaited()
    last_call = transition_mock.call_args_list[-1]
    args, _ = last_call
    assert args[0] == 201
    assert args[1] == "blocked"


# ---------------------------------------------------------------------------
# Smoke result file is always written, even on failure paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smoke_result_written_on_failure(tmp_path: Path) -> None:
    """``smoke_result.json`` is written even when the gate fails."""

    async def fake_load_diff(workspace_root: str, *, base_branch: str = "main") -> str:
        return _DIFF_WITH_STUB

    def fake_grep(diff: str) -> StubResult:
        return StubResult(
            violations=[
                Violation(
                    file="api/routes.py",
                    line=5,
                    pattern="raise NotImplementedError",
                    snippet="    raise NotImplementedError(...)",
                )
            ]
        )

    task = MagicMock()
    task.id = 202
    task.title = "T"
    task.description = "D"
    task.base_branch = "main"

    with (
        patch.object(verify, "_load_diff", AsyncMock(side_effect=fake_load_diff)),
        patch.object(verify, "grep_diff_for_stubs", fake_grep),
        patch.object(verify, "transition_task", AsyncMock()),
    ):
        await verify.run_verify_primitives_for_task(
            task=task,
            workspace_root=str(tmp_path),
            attempt=1,
        )

    smoke_file = tmp_path / SMOKE_RESULT_PATH
    assert smoke_file.is_file()
    payload = json.loads(smoke_file.read_text())
    assert payload["ok"] is False
    # The violations from grep_diff_for_stubs are serialised so a human can
    # see why the gate flipped.
    assert payload.get("violations")


# ---------------------------------------------------------------------------
# Route-exercise failure also routes back to CODING on attempt 1.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_failure_loops_back_on_first_attempt(tmp_path: Path) -> None:
    handle = ServerHandle(state="running", base_url="http://127.0.0.1:8080", port=8080)

    async def fake_load_diff(workspace_root: str, *, base_branch: str = "main") -> str:
        return _DIFF_OK

    def fake_grep(diff: str) -> StubResult:
        return StubResult(violations=[])

    async def fake_exercise(routes, *, handle):
        return {
            r: RouteResult(ok=False, status=500, body="boom", reason="runtime_stub_shape")
            for r in routes
        }

    task = MagicMock()
    task.id = 300
    task.title = "T"
    task.description = "D"
    task.base_branch = "main"
    transition_mock = AsyncMock()

    with (
        patch.object(verify, "_load_diff", AsyncMock(side_effect=fake_load_diff)),
        patch.object(verify, "grep_diff_for_stubs", fake_grep),
        patch.object(verify, "boot_dev_server", AsyncMock(return_value=handle)),
        patch.object(verify, "exercise_routes", AsyncMock(side_effect=fake_exercise)),
        patch.object(verify, "inspect_ui", AsyncMock(return_value=UIResult(ok=True))),
        patch.object(verify, "transition_task", transition_mock),
        patch.object(ServerHandle, "teardown", AsyncMock()),
    ):
        result = await verify.run_verify_primitives_for_task(
            task=task,
            workspace_root=str(tmp_path),
            attempt=1,
        )

    assert result.ok is False
    last_call = transition_mock.call_args_list[-1]
    args, _ = last_call
    assert args[1] == "coding"
