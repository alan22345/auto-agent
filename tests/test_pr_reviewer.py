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
# Artefact scope — Phase 5 implementation (LLM-driven PR-as-artefact review).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artefact_scope_invokes_agent_with_submit_pr_review_skill(
    tmp_path: Path,
) -> None:
    """The artefact scope runs a heavy agent whose prompt mentions the
    ``submit-pr-review`` skill name (the seam ADR-015 §12 prescribes for
    gated agent actions). The agent writes ``pr_review.json``; the
    reviewer reads it and returns the result."""

    workspace = tmp_path
    captured_prompts: list[str] = []

    class FakeAgent:
        async def run(self, prompt: str, **_kw):
            captured_prompts.append(prompt)
            # Simulate the agent invoking the submit-pr-review skill.
            (workspace / ".auto-agent").mkdir(exist_ok=True)
            (workspace / ".auto-agent" / "pr_review.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "verdict": "approved",
                        "comments": [],
                    }
                )
            )
            res = AsyncMock()
            res.output = "wrote pr_review.json"
            return res

    with (
        patch.object(pr_reviewer, "_load_pr_diff", AsyncMock(return_value=_DIFF_OK)),
        patch.object(pr_reviewer, "create_agent", lambda *a, **kw: FakeAgent()),
        patch.object(pr_reviewer, "home_dir_for_task", AsyncMock(return_value=None)),
    ):
        result = await run_pr_review(
            task=_FakeTask(),
            workspace_root=str(workspace),
            scope="artefact",
        )

    assert isinstance(result, PRReviewResult)
    assert result.verdict == "approved"
    # The agent prompt must mention the submit-pr-review skill so CC knows
    # which seam to use.
    assert any("submit-pr-review" in p for p in captured_prompts), captured_prompts


@pytest.mark.asyncio
async def test_artefact_scope_returns_comments_when_agent_emits_them(
    tmp_path: Path,
) -> None:
    """An agent-authored ``changes_requested`` verdict propagates back as
    a non-empty ``comments`` list on the result."""

    workspace = tmp_path

    class FakeAgent:
        async def run(self, prompt: str, **_kw):
            (workspace / ".auto-agent").mkdir(exist_ok=True)
            (workspace / ".auto-agent" / "pr_review.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "verdict": "changes_requested",
                        "comments": [
                            {
                                "path": "README.md",
                                "comment": "PR description doesn't mention the feature flag",
                            },
                            {
                                "comment": "Missing tests for the new endpoint",
                            },
                        ],
                    }
                )
            )
            res = AsyncMock()
            res.output = "wrote pr_review.json"
            return res

    with (
        patch.object(pr_reviewer, "_load_pr_diff", AsyncMock(return_value=_DIFF_OK)),
        patch.object(pr_reviewer, "create_agent", lambda *a, **kw: FakeAgent()),
        patch.object(pr_reviewer, "home_dir_for_task", AsyncMock(return_value=None)),
    ):
        result = await run_pr_review(
            task=_FakeTask(),
            workspace_root=str(workspace),
            scope="artefact",
        )

    assert result.verdict == "changes_requested"
    assert len(result.comments) == 2
    blob = json.dumps(result.comments)
    assert "feature flag" in blob


@pytest.mark.asyncio
async def test_artefact_scope_writes_schema_versioned_pr_review(
    tmp_path: Path,
) -> None:
    """The on-disk pr_review.json carries ``schema_version="1"``."""

    workspace = tmp_path

    class FakeAgent:
        async def run(self, prompt: str, **_kw):
            (workspace / ".auto-agent").mkdir(exist_ok=True)
            (workspace / ".auto-agent" / "pr_review.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "verdict": "approved",
                        "comments": [],
                    }
                )
            )
            res = AsyncMock()
            res.output = ""
            return res

    with (
        patch.object(pr_reviewer, "_load_pr_diff", AsyncMock(return_value=_DIFF_OK)),
        patch.object(pr_reviewer, "create_agent", lambda *a, **kw: FakeAgent()),
        patch.object(pr_reviewer, "home_dir_for_task", AsyncMock(return_value=None)),
    ):
        await run_pr_review(
            task=_FakeTask(),
            workspace_root=str(workspace),
            scope="artefact",
        )

    payload = json.loads((workspace / ".auto-agent" / "pr_review.json").read_text())
    assert payload["schema_version"] == "1"


@pytest.mark.asyncio
async def test_artefact_scope_retries_then_escalates_on_missing_file(
    tmp_path: Path,
) -> None:
    """If the agent never writes ``pr_review.json``, the reviewer retries
    once (skills-bridge contract) and then raises so the caller can
    BLOCK the task."""

    workspace = tmp_path

    call_count = {"n": 0}

    class FakeAgent:
        async def run(self, prompt: str, **_kw):
            # Agent never writes the file — call count just tracks retries.
            call_count["n"] += 1
            res = AsyncMock()
            res.output = "(agent forgot to write the file)"
            return res

    with (
        patch.object(pr_reviewer, "_load_pr_diff", AsyncMock(return_value=_DIFF_OK)),
        patch.object(pr_reviewer, "create_agent", lambda *a, **kw: FakeAgent()),
        patch.object(pr_reviewer, "home_dir_for_task", AsyncMock(return_value=None)),
        pytest.raises(pr_reviewer.MissingPRReviewError),
    ):
        await run_pr_review(
            task=_FakeTask(),
            workspace_root=str(workspace),
            scope="artefact",
        )

    # At least 2 attempts (the original + 1 retry) before escalation.
    assert call_count["n"] >= 2


# ---------------------------------------------------------------------------
# Address-own-comments — one round bound.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_address_own_comments_runs_exactly_one_turn(tmp_path: Path) -> None:
    """When the artefact-scope review returns non-empty comments, a single
    coding turn runs with the comments as input. No second round."""

    workspace = tmp_path
    (workspace / ".auto-agent").mkdir()

    class FakeAgent:
        def __init__(self):
            self.calls: list[str] = []

        async def run(self, prompt: str, **_kw):
            self.calls.append(prompt)
            res = AsyncMock()
            res.output = "patched."
            return res

    fake = FakeAgent()
    with (
        patch.object(pr_reviewer, "create_agent", lambda *a, **kw: fake),
        patch.object(pr_reviewer, "home_dir_for_task", AsyncMock(return_value=None)),
    ):
        await pr_reviewer.address_own_comments(
            task=_FakeTask(),
            workspace_root=str(workspace),
            comments=[
                {"comment": "Add a feature-flag note to the PR description."},
                {"path": "tests/foo.py", "comment": "Cover the empty-list case."},
            ],
        )

    assert len(fake.calls) == 1, "exactly one coding turn"
    # Both comments must reach the agent.
    assert "feature-flag" in fake.calls[0]
    assert "empty-list" in fake.calls[0]


@pytest.mark.asyncio
async def test_address_own_comments_skipped_when_empty(tmp_path: Path) -> None:
    """No comments ⇒ no agent invocation at all."""

    class FakeAgent:
        def __init__(self):
            self.calls = 0

        async def run(self, prompt: str, **_kw):
            self.calls += 1
            return AsyncMock(output="")

    fake = FakeAgent()
    with (
        patch.object(pr_reviewer, "create_agent", lambda *a, **kw: fake),
        patch.object(pr_reviewer, "home_dir_for_task", AsyncMock(return_value=None)),
    ):
        await pr_reviewer.address_own_comments(
            task=_FakeTask(),
            workspace_root=str(tmp_path),
            comments=[],
        )

    assert fake.calls == 0


@pytest.mark.asyncio
async def test_artefact_scope_still_raises_for_unknown_scope(
    tmp_path: Path,
) -> None:
    """Unknown scopes still raise ``ValueError`` — defensive."""

    with pytest.raises(ValueError):
        await run_pr_review(
            task=_FakeTask(),
            workspace_root=str(tmp_path),
            scope="banana",  # type: ignore[arg-type]
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
