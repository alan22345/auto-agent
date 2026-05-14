"""PR-reviewer agent role — ADR-015 §5 Phase 4 correctness scope.

For simple-classified tasks the PR reviewer is the only full verify gate
the flow has (no plan-approval, no final review), so it runs the shared
``verify_primitives.*`` against the PR diff end-to-end:

  1. ``grep_diff_for_stubs(diff)`` — block on no-defer violations.
  2. ``boot_dev_server`` + ``exercise_routes(routes)`` against routes
     inferred from the diff.
  3. ``inspect_ui`` for any UI route touched.

The agent then writes ``.auto-agent/pr_review.json`` so the orchestrator
can read it the same way as every other gate file in the skills bridge.

The artefact scope (complex / complex_large variant) is deferred to
Phase 7; the entry point raises a typed ``ScopeNotYetImplemented`` so a
mis-routed call fails loudly rather than producing a misleading
``verdict``.
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle import pr_reviewer
from agent.lifecycle.pr_reviewer import (
    PRReviewResult,
    ScopeNotYetImplemented,
    infer_routes_from_diff,
    run_pr_review,
)
from agent.lifecycle.verify_primitives import (
    RouteResult,
    ServerHandle,
    StubResult,
    UIResult,
    Violation,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures: a minimal "task" object with the attributes pr_reviewer needs.
# ---------------------------------------------------------------------------


class _FakeTask:
    """Plain object so pr_reviewer can read attributes — no DB stubs."""

    def __init__(
        self,
        *,
        task_id: int = 1,
        pr_url: str = "http://gh/pr/1",
        base_branch: str = "main",
        branch_name: str = "feat/x",
    ):
        self.id = task_id
        self.pr_url = pr_url
        self.base_branch = base_branch
        self.branch_name = branch_name
        self.title = "Sample task"
        self.description = "Sample description"


# A clean diff (no stub patterns, one fastapi route touched).
_DIFF_OK = textwrap.dedent(
    """\
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
)


# Stub introduced — should be flagged by grep_diff_for_stubs.
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


# Diff with no inferable routes — just docs / config changes.
_DIFF_NO_ROUTES = textwrap.dedent(
    """\
    diff --git a/README.md b/README.md
    --- a/README.md
    +++ b/README.md
    @@ -1,2 +1,3 @@
     # Project
    +Some new prose explaining the project.
    """
)


# ---------------------------------------------------------------------------
# Route inference — simple regex over FastAPI decorators + Next.js pages.
# ---------------------------------------------------------------------------


def test_infer_routes_from_fastapi_decorator() -> None:
    """A '+@router.get("/widgets")' line ⇒ "/widgets" route."""

    routes = infer_routes_from_diff(_DIFF_OK)
    assert "/widgets" in routes


def test_infer_routes_from_nextjs_page_path() -> None:
    diff = textwrap.dedent(
        """\
        diff --git a/web-next/app/(app)/dashboard/page.tsx b/web-next/app/(app)/dashboard/page.tsx
        new file mode 100644
        --- /dev/null
        +++ b/web-next/app/(app)/dashboard/page.tsx
        @@ -0,0 +1,3 @@
        +export default function Page() {
        +  return <div>hi</div>;
        +}
        """
    )
    routes = infer_routes_from_diff(diff)
    # The (app) group is dropped in the URL by Next.js convention.
    assert "/dashboard" in routes


def test_infer_routes_returns_empty_for_no_match() -> None:
    assert infer_routes_from_diff(_DIFF_NO_ROUTES) == []


# ---------------------------------------------------------------------------
# Correctness scope — pass / fail paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correctness_scope_passes_when_diff_clean_and_routes_ok(tmp_path: Path) -> None:
    """No stubs, routes return 2xx ⇒ verdict=approved, written to pr_review.json."""

    workspace = tmp_path
    workspace.mkdir(exist_ok=True)

    handle = ServerHandle(state="running", base_url="http://127.0.0.1:8080", port=8080)

    async def fake_exercise(routes, *, handle):
        return {r: RouteResult(ok=True, status=200, body="[]") for r in routes}

    with (
        patch.object(pr_reviewer, "_load_pr_diff", AsyncMock(return_value=_DIFF_OK)),
        patch.object(pr_reviewer, "boot_dev_server", AsyncMock(return_value=handle)),
        patch.object(pr_reviewer, "exercise_routes", AsyncMock(side_effect=fake_exercise)),
        patch.object(pr_reviewer, "inspect_ui", AsyncMock(return_value=UIResult(ok=True))),
        patch.object(ServerHandle, "teardown", AsyncMock()),
    ):
        result = await run_pr_review(
            task=_FakeTask(),
            workspace_root=str(workspace),
            scope="correctness",
        )

    assert isinstance(result, PRReviewResult)
    assert result.verdict == "approved", result
    # The pr_review.json file is written by the function (Python-side, not via
    # the agent skill — see comment in run_pr_review).
    pr_review_path = workspace / ".auto-agent" / "pr_review.json"
    assert pr_review_path.is_file()
    payload = json.loads(pr_review_path.read_text())
    assert payload["schema_version"] == "1"
    assert payload["verdict"] == "approved"


@pytest.mark.asyncio
async def test_correctness_scope_fails_on_notimplementederror_stub(tmp_path: Path) -> None:
    """A `raise NotImplementedError` in a non-test file ⇒ verdict=changes_requested."""

    workspace = tmp_path
    handle = ServerHandle(state="running", base_url="http://127.0.0.1:8080", port=8080)

    async def fake_exercise(routes, *, handle):
        return {r: RouteResult(ok=True, status=200, body="[]") for r in routes}

    with (
        patch.object(pr_reviewer, "_load_pr_diff", AsyncMock(return_value=_DIFF_WITH_STUB)),
        patch.object(pr_reviewer, "boot_dev_server", AsyncMock(return_value=handle)),
        patch.object(pr_reviewer, "exercise_routes", AsyncMock(side_effect=fake_exercise)),
        patch.object(pr_reviewer, "inspect_ui", AsyncMock(return_value=UIResult(ok=True))),
        patch.object(ServerHandle, "teardown", AsyncMock()),
    ):
        result = await run_pr_review(
            task=_FakeTask(),
            workspace_root=str(workspace),
            scope="correctness",
        )

    assert result.verdict == "changes_requested"
    # The reason must mention the stub so a human-on-block can see it.
    blob = json.dumps(result.comments)
    assert "NotImplementedError" in blob or "stub" in blob.lower()


@pytest.mark.asyncio
async def test_correctness_scope_fails_when_route_returns_5xx(tmp_path: Path) -> None:
    """A 500 from an affected route ⇒ verdict=changes_requested."""

    workspace = tmp_path
    handle = ServerHandle(state="running", base_url="http://127.0.0.1:8080", port=8080)

    async def fake_exercise(routes, *, handle):
        return {
            r: RouteResult(ok=False, status=500, body="boom", reason="runtime_stub_shape")
            for r in routes
        }

    with (
        patch.object(pr_reviewer, "_load_pr_diff", AsyncMock(return_value=_DIFF_OK)),
        patch.object(pr_reviewer, "boot_dev_server", AsyncMock(return_value=handle)),
        patch.object(pr_reviewer, "exercise_routes", AsyncMock(side_effect=fake_exercise)),
        patch.object(pr_reviewer, "inspect_ui", AsyncMock(return_value=UIResult(ok=True))),
        patch.object(ServerHandle, "teardown", AsyncMock()),
    ):
        result = await run_pr_review(
            task=_FakeTask(),
            workspace_root=str(workspace),
            scope="correctness",
        )

    assert result.verdict == "changes_requested"
    blob = json.dumps(result.comments)
    assert "/widgets" in blob


@pytest.mark.asyncio
async def test_correctness_scope_skips_route_exercise_with_no_routes(tmp_path: Path) -> None:
    """A diff that touches no routes ⇒ no exercise_routes call, verdict passes if diff clean."""

    workspace = tmp_path

    exercise_mock = AsyncMock()
    boot_mock = AsyncMock(return_value=ServerHandle.disabled())

    with (
        patch.object(pr_reviewer, "_load_pr_diff", AsyncMock(return_value=_DIFF_NO_ROUTES)),
        patch.object(pr_reviewer, "boot_dev_server", boot_mock),
        patch.object(pr_reviewer, "exercise_routes", exercise_mock),
        patch.object(pr_reviewer, "inspect_ui", AsyncMock(return_value=UIResult(ok=True))),
        patch.object(ServerHandle, "teardown", AsyncMock()),
    ):
        result = await run_pr_review(
            task=_FakeTask(),
            workspace_root=str(workspace),
            scope="correctness",
        )

    assert result.verdict == "approved"
    # The whole route-exercise step is skipped when nothing was inferred.
    exercise_mock.assert_not_called()


@pytest.mark.asyncio
async def test_correctness_scope_writes_schema_versioned_pr_review(tmp_path: Path) -> None:
    """The pr_review.json carries schema_version='1' regardless of verdict."""

    workspace = tmp_path
    handle = ServerHandle(state="running", base_url="http://127.0.0.1:8080", port=8080)

    async def fake_exercise(routes, *, handle):
        return {r: RouteResult(ok=True, status=200, body="[]") for r in routes}

    with (
        patch.object(pr_reviewer, "_load_pr_diff", AsyncMock(return_value=_DIFF_OK)),
        patch.object(pr_reviewer, "boot_dev_server", AsyncMock(return_value=handle)),
        patch.object(pr_reviewer, "exercise_routes", AsyncMock(side_effect=fake_exercise)),
        patch.object(pr_reviewer, "inspect_ui", AsyncMock(return_value=UIResult(ok=True))),
        patch.object(ServerHandle, "teardown", AsyncMock()),
    ):
        await run_pr_review(
            task=_FakeTask(),
            workspace_root=str(workspace),
            scope="correctness",
        )

    payload = json.loads((workspace / ".auto-agent" / "pr_review.json").read_text())
    assert payload["schema_version"] == "1"
    assert payload["verdict"] in {"approved", "changes_requested"}


# ---------------------------------------------------------------------------
# Artefact scope — deferred to Phase 7, raises typed sentinel.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artefact_scope_raises_typed_sentinel(tmp_path: Path) -> None:
    """The artefact scope is a known unimplemented branch — callers must
    not invoke it from the simple-flow wiring until Phase 7."""

    with pytest.raises(ScopeNotYetImplemented):
        await run_pr_review(
            task=_FakeTask(),
            workspace_root=str(tmp_path),
            scope="artefact",
        )


# ---------------------------------------------------------------------------
# Direct exercise of the grep wiring — the deletion test for layer 4.
# ---------------------------------------------------------------------------


def test_grep_diff_for_stubs_is_what_the_reviewer_calls() -> None:
    """The diff-level grep path must keep using the shared verify primitive.

    Re-implementing the patterns inside pr_reviewer would split the no-defer
    enforcement across two modules — ADR-015 §8 explicitly says the same
    primitive runs in all four gates.
    """

    from agent.lifecycle import verify_primitives

    result: StubResult = verify_primitives.grep_diff_for_stubs(_DIFF_WITH_STUB)
    assert any(
        isinstance(v, Violation) and "NotImplementedError" in v.pattern for v in result.violations
    )
