# Auto-Heal Loop — Phase 2: Differential Verifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A regression guard that boots a base workspace and a branch workspace, exercises the same routes on each, and reports whether observable behavior diverged — so a health fix that changes behavior is rejected.

**Architecture:** Pure diff functions (`compare_route`, `diff_results`) + one async orchestrator (`differential_verify`) that reuses `agent/lifecycle/verify_primitives.py` (`boot_dev_server`, `exercise_routes`, `ServerHandle`, `RouteResult`). All async I/O is via module-level names the tests patch (matching `tests/test_pr_reviewer.py` style), so no real servers boot in tests.

**Tech Stack:** Python 3.12, async, dataclasses, pytest + `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-06-09-auto-heal-loop-design.md` (component 5c). Prior phase: `agent/health_loop/findings.py`.

**Scope note:** Phase 2 = **route-response** differential only (status + body + boot-state). UI screenshot diffing is **Phase 2b** (needs a screenshot-capture/compare primitive `inspect_ui` doesn't expose). Out of scope here.

---

## File structure

- **Create:** `agent/health_loop/differential.py` — `RouteDiff`, `DifferentialResult`, `compare_route`, `diff_results`, `differential_verify`. One responsibility: decide regress / no-regress from two workspaces.
- **Create:** `tests/test_health_loop_differential.py` — unit tests (all mocked; no real servers).

### Reference: the verify_primitives API this phase consumes

```python
# agent/lifecycle/verify_primitives.py
@dataclass
class ServerHandle:
    state: Literal["running", "disabled", "failed"] = "disabled"
    base_url: str = ""
    port: int = 0
    async def teardown(self) -> None: ...   # idempotent

@dataclass
class RouteResult:
    ok: bool
    status: int = 0
    body: str = ""
    reason: str = ""

async def boot_dev_server(*, workspace: str, repo_id: int | None = None) -> ServerHandle: ...
async def exercise_routes(routes: list[str], *, handle: ServerHandle) -> dict[str, RouteResult]: ...
```

---

### Task 1: result types + `compare_route` (pure)

**Files:**
- Create: `agent/health_loop/differential.py`
- Test: `tests/test_health_loop_differential.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_health_loop_differential.py`:

```python
"""Phase 2 — route-response differential verifier."""
from __future__ import annotations

from agent.lifecycle.verify_primitives import RouteResult
from agent.health_loop.differential import RouteDiff, compare_route


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_differential.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.health_loop.differential'`

- [ ] **Step 3: Implement the types + `compare_route`**

Create `agent/health_loop/differential.py`:

```python
"""Differential (before/after) regression guard for health fixes.

A health fix is supposed to be behavior-preserving, so we boot the base
workspace (cleanup tip, pre-fix) and the branch workspace (post-fix),
exercise the same routes on each, and diff the responses. *Any* observable
divergence — a changed status, a changed body, or a changed boot state —
is treated as a regression and rejects the fix.

Body comparison is JSON-aware: responses that parse as JSON are compared
by structure+value (so key ordering and whitespace don't false-positive),
falling back to stripped-text equality otherwise. Nondeterministic fields
(timestamps, ids) are a known false-positive source handled by per-route
ignore lists in a later phase — for now any value change counts.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from agent.lifecycle.verify_primitives import (
    RouteResult,
    ServerHandle,
    boot_dev_server,
    exercise_routes,
)


@dataclass
class RouteDiff:
    """One observable divergence on a single route."""

    route: str
    detail: str


@dataclass
class DifferentialResult:
    """Outcome of a differential run. ``regressed`` is the gate."""

    regressed: bool
    diffs: list[RouteDiff] = field(default_factory=list)
    note: str = ""


def _normalize_body(body: str):
    """Parse JSON if possible (order-insensitive), else stripped text."""
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body.strip()


def compare_route(route: str, base: RouteResult, branch: RouteResult) -> RouteDiff | None:
    """Return a :class:`RouteDiff` if the two responses diverge, else None.

    Status is compared exactly; body is compared JSON-aware (see
    :func:`_normalize_body`).
    """
    if base.status != branch.status:
        return RouteDiff(
            route=route,
            detail=f"status changed: {base.status} → {branch.status}",
        )
    if _normalize_body(base.body) != _normalize_body(branch.body):
        return RouteDiff(
            route=route,
            detail=f"body changed for {route}",
        )
    return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_differential.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add agent/health_loop/differential.py tests/test_health_loop_differential.py
git commit -m "feat(health-loop): differential result types + compare_route"
```

---

### Task 2: `diff_results` — compare two route-result maps

**Files:**
- Modify: `agent/health_loop/differential.py`
- Test: `tests/test_health_loop_differential.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health_loop_differential.py`:

```python
from agent.health_loop.differential import diff_results


def test_diff_results_flags_changed_route_only():
    base = {
        "/a": RouteResult(ok=True, status=200, body="x"),
        "/b": RouteResult(ok=True, status=200, body="y"),
    }
    branch = {
        "/a": RouteResult(ok=True, status=200, body="x"),       # unchanged
        "/b": RouteResult(ok=True, status=500, body="y"),       # changed
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_differential.py -k diff_results -q`
Expected: FAIL — `ImportError: cannot import name 'diff_results'`

- [ ] **Step 3: Implement `diff_results`**

Add to `agent/health_loop/differential.py` (after `compare_route`):

```python
def diff_results(
    base: dict[str, RouteResult],
    branch: dict[str, RouteResult],
) -> list[RouteDiff]:
    """Diff two route→result maps over the union of their routes.

    A route present on one side but not the other is itself a divergence
    (the change added or removed an observable surface). Order is the
    sorted route order for determinism.
    """
    diffs: list[RouteDiff] = []
    for route in sorted(set(base) | set(branch)):
        b = base.get(route)
        r = branch.get(route)
        if b is None or r is None:
            present = "base" if b is not None else "branch"
            diffs.append(RouteDiff(route=route, detail=f"route missing on the other side (only in {present})"))
            continue
        d = compare_route(route, b, r)
        if d is not None:
            diffs.append(d)
    return diffs
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_differential.py -k diff_results -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/health_loop/differential.py tests/test_health_loop_differential.py
git commit -m "feat(health-loop): diff_results over route maps"
```

---

### Task 3: `differential_verify` — boot both, exercise, diff

**Files:**
- Modify: `agent/health_loop/differential.py`
- Test: `tests/test_health_loop_differential.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health_loop_differential.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch

from agent.health_loop import differential
from agent.health_loop.differential import differential_verify


def _running(url="http://127.0.0.1:9000"):
    return ServerHandle(state="running", base_url=url, port=9000)


# Need ServerHandle in scope for the helpers above.
from agent.lifecycle.verify_primitives import ServerHandle  # noqa: E402


@pytest.mark.asyncio
async def test_verify_no_regression_when_responses_match():
    same = {"/a": RouteResult(ok=True, status=200, body="x")}
    with (
        patch.object(differential, "boot_dev_server", AsyncMock(return_value=_running())),
        patch.object(differential, "exercise_routes", AsyncMock(return_value=same)),
        patch.object(ServerHandle, "teardown", AsyncMock()),
    ):
        result = await differential_verify(
            base_workspace="/tmp/base", branch_workspace="/tmp/branch", routes=["/a"],
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
            base_workspace="/tmp/base", branch_workspace="/tmp/branch", routes=["/a"],
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
            base_workspace="/tmp/base", branch_workspace="/tmp/branch", routes=["/a"],
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
            base_workspace="/tmp/base", branch_workspace="/tmp/branch", routes=["/a"],
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
            base_workspace="/tmp/base", branch_workspace="/tmp/branch", routes=["/a"],
        )
    # Booted twice (base + branch) ⇒ torn down twice.
    assert teardown.await_count == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_differential.py -k verify -q`
Expected: FAIL — `ImportError: cannot import name 'differential_verify'`

- [ ] **Step 3: Implement `differential_verify`**

Add to `agent/health_loop/differential.py`:

```python
async def _boot_and_exercise(
    workspace: str, routes: list[str], repo_id: int | None
) -> tuple[ServerHandle, dict[str, RouteResult]]:
    """Boot one workspace and exercise ``routes`` against it.

    Returns the handle (caller owns teardown) and the route results. If the
    server is not ``running`` the results are empty.
    """
    handle = await boot_dev_server(workspace=workspace, repo_id=repo_id)
    if handle.state != "running":
        return handle, {}
    results = await exercise_routes(routes, handle=handle)
    return handle, results


async def differential_verify(
    *,
    base_workspace: str,
    branch_workspace: str,
    routes: list[str],
    repo_id: int | None = None,
) -> DifferentialResult:
    """Boot base + branch, exercise ``routes`` on each, and diff.

    ``regressed`` is True when any route response diverges OR the boot
    state diverges (one side boots, the other doesn't). When neither side
    boots there is no observable surface to compare — a degenerate
    no-regression with an explanatory note. Both servers are always torn
    down.
    """
    base_handle, base_results = await _boot_and_exercise(base_workspace, routes, repo_id)
    try:
        branch_handle, branch_results = await _boot_and_exercise(branch_workspace, routes, repo_id)
        try:
            base_up = base_handle.state == "running"
            branch_up = branch_handle.state == "running"

            if base_up != branch_up:
                return DifferentialResult(
                    regressed=True,
                    note=(
                        f"boot state diverged: base="
                        f"{base_handle.state}, branch={branch_handle.state}"
                    ),
                )
            if not base_up and not branch_up:
                return DifferentialResult(
                    regressed=False,
                    note="neither workspace booted a dev server; no routes compared",
                )

            diffs = diff_results(base_results, branch_results)
            return DifferentialResult(regressed=bool(diffs), diffs=diffs)
        finally:
            await branch_handle.teardown()
    finally:
        await base_handle.teardown()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_differential.py -k verify -q`
Expected: PASS

- [ ] **Step 5: Run the full Phase 2 test file + lint + format**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_differential.py -q`
Expected: PASS (all)
Run: `.venv/bin/ruff check agent/health_loop/ tests/test_health_loop_differential.py`
Expected: `All checks passed!`
Run: `.venv/bin/ruff format --check agent/health_loop/differential.py tests/test_health_loop_differential.py`
Expected: `2 files already formatted`

- [ ] **Step 6: Commit**

```bash
git add agent/health_loop/differential.py tests/test_health_loop_differential.py
git commit -m "feat(health-loop): differential_verify boots base+branch and diffs"
```

---

### Phase 2 exit criteria

- `agent/health_loop/differential.py` exposes `RouteDiff`, `DifferentialResult`,
  `compare_route`, `diff_results`, `differential_verify`.
- `differential_verify` is pure-orchestration over patchable `boot_dev_server` /
  `exercise_routes`; fully unit-tested with mocks (no real servers), ruff + format
  clean.
- Both servers are always torn down (even on the regression early-returns).

### Phase 2b (deferred, separate plan)
UI screenshot differential: add a screenshot-capture primitive (or extend
`inspect_ui` to return a comparable artifact) and a vision-LLM "materially
different?" comparator; fold its verdict into `DifferentialResult`.
```
