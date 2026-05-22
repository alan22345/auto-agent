# Repo-Graph Capability / Flow Map — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Derive named flows (entry-point → terminal traces) from existing `RepoGraph.graph_json`, persist them in a new nullable `flow_json` column, expose a recompute endpoint, and ship a `which_capability` agent op. No UI, no LLM labelling — those land in Phases 2-5.

**Architecture:** A new module `agent/graph_analyzer/flows.py` (with `entry_points.py` alongside it) walks the existing `RepoGraphBlob.edges` forward from detected entry-point nodes, terminating at side-effect calls. Output is a Pydantic `FlowJsonBlob` persisted to `RepoGraph.flow_json` (new JSONB column). One endpoint kicks recompute; one new op on `query_repo_graph` reads from the persisted blob.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Alembic, Pydantic, pytest. Same toolset as the existing graph pipeline.

**Reference spec:** [docs/superpowers/specs/2026-05-22-repo-graph-capability-flow-map-design.md](../specs/2026-05-22-repo-graph-capability-flow-map-design.md). When in doubt about field semantics, defer to the spec.

**Phase 1 boundary discipline (deliberate handoffs to Phase 2):**

- `Capability.name`, `Capability.description`, `Flow.name`, `Flow.description` are persisted as `None` in Phase 1. Phase 2 populates them via an LLM call.
- `Flow.file_set_hash` and `Capability.flow_membership_hash` are computed and stored in Phase 1 (cheap, deterministic). Phase 2 reads them to decide whether to re-label.
- `Capability.id` in Phase 1 is the single literal `"unlabeled"`. All flows belong to that one capability until Phase 2 groups them.

The `which_capability` agent op is wired with the Phase 1 shape and returns `capability_name: None` until Phase 2 labels exist. The endpoint and the agent op MUST work end-to-end against this Phase 1 shape — no stubs, no 501s.

---

## File structure (final shape after Phase 1)

**New files:**

- `agent/graph_analyzer/entry_points.py` — entry-point detection over a `RepoGraphBlob`. One pure function `detect_entry_points(blob) -> list[EntryPoint]`.
- `agent/graph_analyzer/flows.py` — forward trace + branch/cycle/terminal handling + `derive_flow_blob(graph_blob) -> FlowJsonBlob`.
- `migrations/versions/053_repo_graph_flow_json.py` — adds `flow_json JSONB NULL` to `repo_graphs`.
- `tests/test_graph_entry_points.py` — entry-point detection unit tests.
- `tests/test_graph_flows_trace.py` — forward-trace + branch/cycle + terminal tests.
- `tests/test_graph_flows_derive.py` — top-level `derive_flow_blob` tests.
- `tests/test_graph_flow_json_migration.py` — schema migration round-trip.
- `tests/test_repo_graph_flows_endpoint.py` — recompute endpoint tests.
- `tests/test_query_repo_graph_which_capability.py` — agent op tests.
- `tests/test_graph_flows_e2e.py` — fixture-driven end-to-end smoke.

**Modified files:**

- `shared/types.py` — append `EntryPointKind`, `EntryPoint`, `FlowStep`, `Flow`, `Capability`, `FlowJsonBlob` Pydantic models.
- `shared/models/core.py` — add `flow_json = Column(JSONB, nullable=True)` to `RepoGraph`.
- `agent/tools/query_repo_graph.py` — add `"which_capability"` to `_KNOWN_OPS`, branch + dispatch helper.
- `orchestrator/router.py` — add `POST /api/repos/{repo_id}/graph/flows/recompute` near the existing graph endpoints (around line 3415).

---

## Task 1 — Pydantic schema for the flow JSON blob

**Files:**
- Modify: `shared/types.py` (append at end of code-graph block, after `LatestRepoGraphData`)
- Test: `tests/test_graph_flow_json_schema.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_graph_flow_json_schema.py`:

```python
"""Schema-shape tests for the FlowJsonBlob Pydantic models (Phase 1).

Phase 1 leaves name/description as None; Phase 2 will populate them.
The schema must accept both shapes so a Phase-1-written blob round-trips
through a Phase-2-aware deserialiser.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.types import (
    Capability,
    EntryPoint,
    EntryPointKind,
    Flow,
    FlowJsonBlob,
    FlowStep,
)


def test_entry_point_kind_literal_values():
    # All four kinds defined in the spec §3 step 1
    for kind in ("http", "queue", "cron", "cli"):
        ep = EntryPoint(node_id="m.f", kind=kind)  # type: ignore[arg-type]
        assert ep.kind == kind


def test_entry_point_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        EntryPoint(node_id="m.f", kind="websocket")  # type: ignore[arg-type]


def test_flow_step_minimum_shape():
    step = FlowStep(node_id="m.f", depth=0)
    assert step.depth == 0
    assert step.is_branch_root is False  # default
    assert step.is_cycle_back is False


def test_flow_phase1_shape_allows_null_label():
    flow = Flow(
        id="auth_login_a1b2",
        entry_point=EntryPoint(node_id="api.login", kind="http"),
        terminal_node_id="api.login",
        terminal_kind="response",
        steps=[FlowStep(node_id="api.login", depth=0)],
        file_set=["api/login.py"],
        file_set_hash="sha256:abc",
        name=None,
        description=None,
    )
    assert flow.name is None
    assert flow.description is None


def test_capability_phase1_unlabeled_id():
    cap = Capability(
        id="unlabeled",
        flow_ids=["auth_login_a1b2"],
        flow_membership_hash="sha256:def",
        name=None,
        description=None,
    )
    assert cap.id == "unlabeled"
    assert cap.name is None


def test_flow_json_blob_round_trip():
    blob = FlowJsonBlob(
        capabilities=[
            Capability(
                id="unlabeled",
                flow_ids=["auth_login_a1b2"],
                flow_membership_hash="sha256:def",
                name=None,
                description=None,
            ),
        ],
        flows=[
            Flow(
                id="auth_login_a1b2",
                entry_point=EntryPoint(node_id="api.login", kind="http"),
                terminal_node_id="api.login",
                terminal_kind="response",
                steps=[FlowStep(node_id="api.login", depth=0)],
                file_set=["api/login.py"],
                file_set_hash="sha256:abc",
                name=None,
                description=None,
            ),
        ],
        unreached=["m.helper"],
        derived_at_commit="sha:7e9f",
        deriver_version="phase1",
    )
    again = FlowJsonBlob.model_validate(blob.model_dump())
    assert again == blob
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_graph_flow_json_schema.py -v`

Expected: ImportError on `shared.types.FlowJsonBlob` (and friends) — none of these names exist yet.

- [ ] **Step 3: Add the Pydantic models**

Open `shared/types.py`, find the end of the existing code-graph block (just after the `LatestRepoGraphData` class around line 368). Insert before the next non-graph section:

```python
# --- Capability / flow derivation (Phase 1 of capability-flow map spec) ---
#
# A *flow* is one forward-trace from a detected entry point to a terminal
# side effect, recorded over the nodes/edges in `RepoGraphBlob`. A
# *capability* is a named group of flows. Phase 1 derives flows; Phase 2
# labels them via an LLM call. The shape supports both phases: name and
# description fields are nullable so Phase 1 blobs round-trip through a
# Phase 2-aware deserialiser.

EntryPointKind = Literal["http", "queue", "cron", "cli"]
TerminalKind = Literal["response", "queue_publish", "external_http", "db_write", "none"]


class EntryPoint(BaseModel):
    """One node detected as a flow entry point — see spec §3 step 1."""

    node_id: str
    kind: EntryPointKind


class FlowStep(BaseModel):
    """One node on a flow's forward trace.

    ``depth`` is the BFS distance from the entry point along the dominant
    path; branch nodes carry the branch root's depth. ``is_branch_root``
    flags a node that fans out into multiple outgoing call edges at the
    same depth (rendered as a branch fork in Phase 3). ``is_cycle_back``
    marks the back-edge target when a cycle was detected and the trace
    stopped without re-expanding (spec §3 step 4).
    """

    node_id: str
    depth: int
    is_branch_root: bool = False
    is_cycle_back: bool = False


class Flow(BaseModel):
    """One flow — entry point through forward trace to a terminal effect.

    ``name`` and ``description`` are produced by the Phase 2 LLM labeller;
    Phase 1 leaves them ``None``. ``file_set_hash`` is the SHA-256 of the
    sorted-by-path file contents that make up this flow's ``file_set``;
    Phase 2 uses it to skip re-labelling unchanged flows (spec §4).
    """

    id: str
    entry_point: EntryPoint
    terminal_node_id: str
    terminal_kind: TerminalKind
    steps: list[FlowStep]
    file_set: list[str]
    file_set_hash: str
    name: str | None = None
    description: str | None = None


class Capability(BaseModel):
    """One named capability — a group of related flows.

    Phase 1 emits exactly one capability with ``id="unlabeled"`` covering
    every derived flow. Phase 2 groups flows into ~5-12 capabilities and
    populates ``name`` / ``description``. ``flow_membership_hash`` is the
    SHA-256 of the sorted ``flow_ids`` list; Phase 2 skips re-labelling
    capabilities whose membership hash matches the persisted value.
    """

    id: str
    flow_ids: list[str]
    flow_membership_hash: str
    name: str | None = None
    description: str | None = None


class FlowJsonBlob(BaseModel):
    """Full capability/flow derivation result — payload of
    ``RepoGraph.flow_json``.

    ``unreached`` is the list of node ids in the underlying graph that
    no flow's forward trace touched. Surfaced as the Unreached tray in
    the Phase 3 UI (spec §3 step 6).
    """

    capabilities: list[Capability]
    flows: list[Flow]
    unreached: list[str]
    derived_at_commit: str
    deriver_version: str
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_graph_flow_json_schema.py -v`

Expected: all 6 tests PASS.

- [ ] **Step 5: Lint**

Run: `ruff check shared/types.py tests/test_graph_flow_json_schema.py`

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add shared/types.py tests/test_graph_flow_json_schema.py
git commit -m "feat(graph): FlowJsonBlob Pydantic schema for capability/flow map (Phase 1)"
```

---

## Task 2 — Migration 053: add `flow_json` JSONB column

**Files:**
- Create: `migrations/versions/053_repo_graph_flow_json.py`
- Test: `tests/test_graph_flow_json_migration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_graph_flow_json_migration.py`:

```python
"""Migration 053 adds a nullable flow_json JSONB column to repo_graphs.

Verified two ways:
  - the ORM column attribute exists on shared.models.core.RepoGraph and
    is nullable;
  - the live DB schema (after migrations run) has the column with the
    JSONB type and nullable=true.
"""
from __future__ import annotations

import sqlalchemy as sa

from shared.database import async_session
from shared.models.core import RepoGraph


def test_orm_has_nullable_flow_json_column():
    col = RepoGraph.__table__.columns["flow_json"]
    assert col.nullable is True
    # JSONB is the SQLAlchemy dialect-specific type; ORM-level we accept
    # any subclass of sa.JSON.
    assert isinstance(col.type, sa.JSON)


async def test_db_has_flow_json_column():
    async with async_session() as session:
        result = await session.execute(
            sa.text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'repo_graphs' AND column_name = 'flow_json'",
            ),
        )
        row = result.first()
        assert row is not None, "flow_json column missing — run alembic upgrade head"
        data_type, is_nullable = row
        assert data_type == "jsonb"
        assert is_nullable == "YES"
```

Add the `pytestmark` at the top if the project's test conventions require asyncio marking. Check `tests/conftest.py` for the established pattern (most graph tests already use `pytest.mark.asyncio` or auto-mode).

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_graph_flow_json_migration.py -v`

Expected: `KeyError: 'flow_json'` on the ORM test; the DB test fails with "column missing."

- [ ] **Step 3: Create the migration**

Create `migrations/versions/053_repo_graph_flow_json.py`:

```python
"""repo_graph_flow_json

Revision ID: 053
Revises: 052
Create Date: 2026-05-22

Adds a nullable flow_json JSONB column to repo_graphs. Phase 1 of the
capability/flow map spec persists the result of forward-tracing flows
from entry points to terminal side effects. Nullable because existing
RepoGraph rows do not have a derivation; the API surface treats null
as "compute flows now" and a populated blob as "show them."

See docs/superpowers/specs/2026-05-22-repo-graph-capability-flow-map-design.md
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "053"
down_revision = "052"


def upgrade() -> None:
    op.add_column(
        "repo_graphs",
        sa.Column(
            "flow_json",
            postgresql.JSONB(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("repo_graphs", "flow_json")
```

- [ ] **Step 4: Add the ORM column**

Edit `shared/models/core.py` — in the `RepoGraph` class (around line 637), add after the existing `failed_sites` column (line 679):

```python
    # Capability/flow derivation (Phase 1 of capability-flow map spec).
    # Nullable: a freshly-completed analysis has graph_json but no
    # flow_json until the recompute endpoint is hit.
    flow_json = Column(JSONB, nullable=True)
```

- [ ] **Step 5: Run the migration**

Run: `docker compose exec auto-agent alembic upgrade head`

(If working in a non-docker context, use the equivalent local invocation. The user CLAUDE.md mentions docker compose as the canonical path.)

Expected output: applies revision 053.

- [ ] **Step 6: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_graph_flow_json_migration.py -v`

Expected: both tests PASS.

- [ ] **Step 7: Run the full graph test set to confirm no regression**

Run: `.venv/bin/python3 -m pytest tests/ -k "graph" -q`

Expected: no new failures relative to the baseline before this commit.

- [ ] **Step 8: Lint**

Run: `ruff check migrations/versions/053_repo_graph_flow_json.py shared/models/core.py`

Expected: no errors.

- [ ] **Step 9: Commit**

```bash
git add migrations/versions/053_repo_graph_flow_json.py shared/models/core.py tests/test_graph_flow_json_migration.py
git commit -m "feat(graph): migration 053 + ORM column for flow_json (Phase 1)"
```

---

## Task 3 — Entry-point detection

**Files:**
- Create: `agent/graph_analyzer/entry_points.py`
- Test: `tests/test_graph_entry_points.py`

The four kinds from spec §3 step 1 are `http`, `queue`, `cron`, `cli`. Phase 1 detects them via signals already available in `RepoGraphBlob`:

- `http`: a node that is the *target* of any `kind="http"` edge — that's exactly the spec's "incoming http edge" rule and is what the cross-language matching stage already produces.
- `queue`: a node whose name matches `*_worker`, `*_consumer`, `*_handler`, or has a `@app.task` / `@celery.task` / `@worker` decorator in `Node.decorators`.
- `cron`: a node with a decorator matching `@scheduled_*`, `@cron.*`, `@app.scheduled_*`.
- `cli`: a node with name `main` whose file matches `**/cli/**`, `**/__main__.py`, or whose decorators include `@click.command` / `@click.group`.

The detection is heuristic and extensible. Phase 1 ships the four-kind detector against the listed signals; missed entry points fall into the Unreached tray, which is acceptable behaviour.

- [ ] **Step 1: Write the failing test**

Create `tests/test_graph_entry_points.py`:

```python
"""Entry-point detection for the capability/flow map (Phase 1).

Builds synthetic RepoGraphBlobs and asserts the detector returns the
expected EntryPoint list. No real fixtures needed — the detector is a
pure function over the blob.
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent.graph_analyzer.entry_points import detect_entry_points
from shared.types import (
    Edge,
    EdgeEvidence,
    Node,
    RepoGraphBlob,
)


def _make_blob(nodes: list[Node], edges: list[Edge]) -> RepoGraphBlob:
    return RepoGraphBlob(
        commit_sha="0" * 40,
        generated_at=datetime.now(tz=timezone.utc),
        analyser_version="test",
        areas=[],
        nodes=nodes,
        edges=edges,
    )


def _fn(node_id: str, **kwargs) -> Node:
    return Node(
        id=node_id,
        kind="function",
        label=node_id.split("::")[-1],
        file=kwargs.get("file", "src/x.py"),
        area=kwargs.get("area", "src"),
        decorators=kwargs.get("decorators", []),
    )


def _edge(source: str, target: str, kind: str) -> Edge:
    return Edge(
        source=source,
        target=target,
        kind=kind,  # type: ignore[arg-type]
        evidence=EdgeEvidence(file="src/x.py", line=1, snippet="x"),
        source_kind="ast",
    )


def test_http_target_node_is_entry_point():
    nodes = [_fn("api/login.py::login"), _fn("web/login.tsx::handleSubmit")]
    edges = [_edge("web/login.tsx::handleSubmit", "api/login.py::login", "http")]
    eps = detect_entry_points(_make_blob(nodes, edges))
    assert len(eps) == 1
    assert eps[0].node_id == "api/login.py::login"
    assert eps[0].kind == "http"


def test_celery_task_decorator_is_queue_entry_point():
    nodes = [_fn("workers/calc.py::compute", decorators=["@celery.task"])]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert [e.kind for e in eps] == ["queue"]


def test_worker_suffix_name_is_queue_entry_point():
    nodes = [_fn("workers/runner.py::report_worker")]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert [e.kind for e in eps] == ["queue"]


def test_scheduled_decorator_is_cron_entry_point():
    nodes = [_fn("jobs/cleanup.py::run", decorators=["@scheduled_task('0 0 * * *')"])]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert [e.kind for e in eps] == ["cron"]


def test_click_command_is_cli_entry_point():
    nodes = [_fn("cli/admin.py::reset_db", decorators=["@click.command()"])]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert [e.kind for e in eps] == ["cli"]


def test_main_in_dunder_main_is_cli_entry_point():
    nodes = [_fn("pkg/__main__.py::main", file="pkg/__main__.py")]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert [e.kind for e in eps] == ["cli"]


def test_function_with_no_signals_is_not_entry_point():
    nodes = [_fn("src/lib/helpers.py::format_date")]
    eps = detect_entry_points(_make_blob(nodes, []))
    assert eps == []


def test_one_node_matches_at_most_one_kind():
    # If a function has both an http-edge target and a celery.task
    # decorator (unusual but legal), http wins (more specific signal).
    nodes = [_fn("api/x.py::handler", decorators=["@celery.task"])]
    edges = [_edge("web/x.tsx::call", "api/x.py::handler", "http")]
    eps = detect_entry_points(_make_blob(nodes, edges))
    assert len(eps) == 1
    assert eps[0].kind == "http"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_graph_entry_points.py -v`

Expected: ImportError on `agent.graph_analyzer.entry_points`.

- [ ] **Step 3: Implement the detector**

Create `agent/graph_analyzer/entry_points.py`:

```python
"""Entry-point detection for the capability/flow map (Phase 1).

Given a finished :class:`shared.types.RepoGraphBlob`, return the list of
nodes that should be treated as flow entry points. Four kinds in v1:

* ``http``   — target of an incoming ``kind="http"`` edge (already
  produced by ADR-016 Phase 4 cross-language matching).
* ``queue``  — Celery / RQ / dramatiq decorator OR function name
  matching the ``*_worker``/``*_consumer``/``*_handler`` convention.
* ``cron``   — scheduled-job decorator (``@scheduled_*``, ``@cron.*``,
  ``@app.scheduled_*``).
* ``cli``    — Click decorator OR ``main`` in ``__main__.py`` OR
  ``main`` in a ``cli/`` directory.

When a single node matches multiple signals (e.g. an HTTP handler with
a Celery decorator), the most-specific signal wins; the precedence is
``http > queue > cron > cli``.

This is pure: no I/O, no DB, no LLM. Heuristics are easy to extend —
add a kind by adding a clause; missed entry points land in the
Unreached tray downstream.
"""
from __future__ import annotations

import re

from shared.types import EntryPoint, EntryPointKind, Node, RepoGraphBlob

_QUEUE_DECORATOR_RE = re.compile(
    r"^@(?:celery\.task|app\.task|dramatiq\.actor|rq\.job|worker(?:\.\w+)?)\b",
)
_QUEUE_NAME_RE = re.compile(r"_(worker|consumer|handler)$")
_CRON_DECORATOR_RE = re.compile(
    r"^@(?:scheduled_\w+|cron\.\w+|app\.scheduled_\w+|periodic_task)\b",
)
_CLI_DECORATOR_RE = re.compile(r"^@(?:click\.command|click\.group|app\.command)\b")
_DUNDER_MAIN_RE = re.compile(r"(?:^|/)__main__\.py$")
_CLI_DIR_RE = re.compile(r"(?:^|/)cli/")


def _is_http_entry(node: Node, http_targets: set[str]) -> bool:
    return node.id in http_targets


def _is_queue_entry(node: Node) -> bool:
    if any(_QUEUE_DECORATOR_RE.match(d) for d in node.decorators):
        return True
    return bool(_QUEUE_NAME_RE.search(node.label))


def _is_cron_entry(node: Node) -> bool:
    return any(_CRON_DECORATOR_RE.match(d) for d in node.decorators)


def _is_cli_entry(node: Node) -> bool:
    if any(_CLI_DECORATOR_RE.match(d) for d in node.decorators):
        return True
    if node.label != "main":
        return False
    if not node.file:
        return False
    if _DUNDER_MAIN_RE.search(node.file):
        return True
    if _CLI_DIR_RE.search(node.file):
        return True
    return False


def detect_entry_points(blob: RepoGraphBlob) -> list[EntryPoint]:
    """Return all entry-point nodes in *blob*, deduped by ``node_id``."""
    http_targets: set[str] = {e.target for e in blob.edges if e.kind == "http"}

    result: list[EntryPoint] = []
    seen: set[str] = set()

    def _add(node: Node, kind: EntryPointKind) -> None:
        if node.id in seen:
            return
        result.append(EntryPoint(node_id=node.id, kind=kind))
        seen.add(node.id)

    # Precedence: http > queue > cron > cli.
    for node in blob.nodes:
        if node.kind != "function":
            continue
        if _is_http_entry(node, http_targets):
            _add(node, "http")
    for node in blob.nodes:
        if node.kind != "function":
            continue
        if _is_queue_entry(node):
            _add(node, "queue")
    for node in blob.nodes:
        if node.kind != "function":
            continue
        if _is_cron_entry(node):
            _add(node, "cron")
    for node in blob.nodes:
        if node.kind != "function":
            continue
        if _is_cli_entry(node):
            _add(node, "cli")

    return result


__all__ = ["detect_entry_points"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_graph_entry_points.py -v`

Expected: all 8 tests PASS.

- [ ] **Step 5: Lint**

Run: `ruff check agent/graph_analyzer/entry_points.py tests/test_graph_entry_points.py`

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add agent/graph_analyzer/entry_points.py tests/test_graph_entry_points.py
git commit -m "feat(graph): entry-point detection (http/queue/cron/cli) for flow derivation"
```

---

## Task 4 — Forward trace + branch + cycle handling

**Files:**
- Create: `agent/graph_analyzer/flows.py` (partial — `trace_flow` only this task)
- Test: `tests/test_graph_flows_trace.py`

This task implements a single function `trace_flow(blob, entry_point) -> list[FlowStep]` that walks the call graph forward from the entry point, returning an ordered list of `FlowStep` records. Branch and cycle handling are per spec §3 steps 3-4:

- Walk call edges (`kind="calls"`) only. Ignore `imports`/`inherits`/`http` for trace expansion (the `http` edge already crossed into the entry point).
- BFS-style, but record visit order so the returned list is deterministic.
- Max depth = 50 (spec out-of-scope says max-step cap, default 50, with the tail collapsed). Use a constant `MAX_FLOW_STEPS`.
- A node with ≥2 outgoing call edges is marked `is_branch_root=True`. Both child branches are walked, but only up to depth 3 deeper than the branch root (spec §3 step 3 "depth-3 branch inlining").
- Cycle detection: if a node is about to be visited that is already on the current path, record it as a terminal step with `is_cycle_back=True` and stop expanding.

- [ ] **Step 1: Write the failing test**

Create `tests/test_graph_flows_trace.py`:

```python
"""Forward-trace tests for flow derivation (Phase 1).

trace_flow walks call edges forward from an entry point, capping depth
and collapsing deep branches per spec §3 steps 3-4.
"""
from __future__ import annotations

from datetime import datetime, timezone

from agent.graph_analyzer.flows import MAX_FLOW_STEPS, trace_flow
from shared.types import (
    Edge,
    EdgeEvidence,
    EntryPoint,
    Node,
    RepoGraphBlob,
)


def _blob(nodes, edges):
    return RepoGraphBlob(
        commit_sha="0" * 40,
        generated_at=datetime.now(tz=timezone.utc),
        analyser_version="test",
        areas=[],
        nodes=nodes,
        edges=edges,
    )


def _fn(node_id: str) -> Node:
    return Node(
        id=node_id, kind="function", label=node_id, file=f"{node_id}.py", area="src",
    )


def _call(src: str, dst: str) -> Edge:
    return Edge(
        source=src,
        target=dst,
        kind="calls",
        evidence=EdgeEvidence(file=f"{src}.py", line=1, snippet=f"{dst}()"),
        source_kind="ast",
    )


def test_linear_chain():
    blob = _blob(
        [_fn("a"), _fn("b"), _fn("c")],
        [_call("a", "b"), _call("b", "c")],
    )
    steps = trace_flow(blob, EntryPoint(node_id="a", kind="http"))
    assert [s.node_id for s in steps] == ["a", "b", "c"]
    assert [s.depth for s in steps] == [0, 1, 2]
    assert all(not s.is_branch_root for s in steps)
    assert all(not s.is_cycle_back for s in steps)


def test_branch_marks_root_and_inlines_both_branches():
    blob = _blob(
        [_fn("a"), _fn("b"), _fn("c"), _fn("d")],
        [_call("a", "b"), _call("a", "c"), _call("c", "d")],
    )
    steps = trace_flow(blob, EntryPoint(node_id="a", kind="http"))
    by_id = {s.node_id: s for s in steps}
    assert by_id["a"].is_branch_root is True
    assert by_id["b"].depth == 1
    assert by_id["c"].depth == 1
    assert by_id["d"].depth == 2


def test_cycle_back_edge_is_terminal_not_expanded():
    blob = _blob(
        [_fn("a"), _fn("b")],
        [_call("a", "b"), _call("b", "a")],
    )
    steps = trace_flow(blob, EntryPoint(node_id="a", kind="http"))
    ids = [s.node_id for s in steps]
    # "a" appears twice: at depth 0 (root) and at depth 2 (cycle-back).
    assert ids == ["a", "b", "a"]
    assert steps[-1].is_cycle_back is True


def test_branch_depth_capped_at_three_past_branch_root():
    # Build: a→b, a→c, c→c1→c2→c3→c4 (c4 is past the depth-3 cap)
    nodes = [_fn(x) for x in ("a", "b", "c", "c1", "c2", "c3", "c4")]
    edges = [
        _call("a", "b"),
        _call("a", "c"),
        _call("c", "c1"),
        _call("c1", "c2"),
        _call("c2", "c3"),
        _call("c3", "c4"),
    ]
    blob = _blob(nodes, edges)
    steps = trace_flow(blob, EntryPoint(node_id="a", kind="http"))
    ids = [s.node_id for s in steps]
    # branch root is "a", branch via "c" extends c→c1→c2→c3 (depth-3
    # past the branch root at depth 1) — c4 (depth 5) is dropped.
    assert "c4" not in ids
    assert "c3" in ids


def test_max_flow_steps_cap():
    # A chain longer than MAX_FLOW_STEPS terminates at MAX_FLOW_STEPS.
    n = MAX_FLOW_STEPS + 5
    nodes = [_fn(f"n{i}") for i in range(n)]
    edges = [_call(f"n{i}", f"n{i + 1}") for i in range(n - 1)]
    blob = _blob(nodes, edges)
    steps = trace_flow(blob, EntryPoint(node_id="n0", kind="http"))
    assert len(steps) == MAX_FLOW_STEPS


def test_non_call_edges_are_not_followed():
    blob = _blob(
        [_fn("a"), _fn("b"), _fn("c")],
        [
            _call("a", "b"),
            # 'imports' should NOT be traversed
            Edge(
                source="a",
                target="c",
                kind="imports",
                evidence=EdgeEvidence(file="a.py", line=1, snippet="import c"),
                source_kind="ast",
            ),
        ],
    )
    steps = trace_flow(blob, EntryPoint(node_id="a", kind="http"))
    assert [s.node_id for s in steps] == ["a", "b"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_graph_flows_trace.py -v`

Expected: ImportError on `agent.graph_analyzer.flows.trace_flow`.

- [ ] **Step 3: Implement `trace_flow`**

Create `agent/graph_analyzer/flows.py`:

```python
"""Capability / flow derivation (Phase 1).

Top-level entry point is :func:`derive_flow_blob`, which composes:

  detect_entry_points  →  trace_flow per entry  →  classify_terminal
       →  hash file sets  →  assemble FlowJsonBlob

Phase 1 leaves capability and flow names as ``None`` — Phase 2 labels
them via an LLM call. Phase 1 emits exactly one capability with
``id="unlabeled"`` containing every derived flow.

The trace is pure (no I/O, no DB): given a finished RepoGraphBlob, it
produces a deterministic FlowJsonBlob. The recompute endpoint reads
the blob from the DB, runs derivation, writes the result back. The
file-hash step in :func:`derive_flow_blob` is the only stage that
touches disk.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from shared.types import (
    EntryPoint,
    FlowStep,
    Node,
    RepoGraphBlob,
)

# Spec §10: hard cap on per-flow step count. Anything past this is
# dropped; the UI may render a "+N hidden" marker in Phase 3.
MAX_FLOW_STEPS = 50

# Spec §3 step 3: branches inlined to depth-3 past the branch root.
BRANCH_INLINE_DEPTH = 3


def _outgoing_call_targets(edges_by_source: dict[str, list[str]], node_id: str) -> list[str]:
    return edges_by_source.get(node_id, [])


def trace_flow(blob: RepoGraphBlob, entry_point: EntryPoint) -> list[FlowStep]:
    """Forward-trace call edges from *entry_point* into an ordered step list.

    BFS over ``kind="calls"`` edges, deterministic in the order edges
    appear in the blob. Branches mark their root and are walked up to
    ``BRANCH_INLINE_DEPTH`` past the root depth. Cycles record a
    cycle-back step and stop. The walk hard-caps at ``MAX_FLOW_STEPS``.
    """
    edges_by_source: dict[str, list[str]] = defaultdict(list)
    for edge in blob.edges:
        if edge.kind == "calls":
            edges_by_source[edge.source].append(edge.target)

    nodes_by_id: dict[str, Node] = {n.id: n for n in blob.nodes}

    steps: list[FlowStep] = []
    # Per-step branch-root depth, used to cap depth-3 inlining on
    # branches.  Index parallel to `frontier` entries.
    frontier: list[tuple[str, int, int | None]] = [(entry_point.node_id, 0, None)]
    on_path: set[str] = set()

    while frontier and len(steps) < MAX_FLOW_STEPS:
        node_id, depth, branch_root_depth = frontier.pop(0)
        if node_id in on_path:
            # Cycle: emit a cycle-back terminal step but do not expand.
            steps.append(FlowStep(node_id=node_id, depth=depth, is_cycle_back=True))
            continue

        targets = _outgoing_call_targets(edges_by_source, node_id)
        is_branch_root = len(targets) >= 2
        steps.append(
            FlowStep(node_id=node_id, depth=depth, is_branch_root=is_branch_root),
        )
        on_path.add(node_id)

        if not targets:
            continue

        # Determine the branch-root depth that governs the children's
        # depth cap. If the current node is itself a branch root, its
        # children inherit *this* node's depth as the new root.
        new_root_depth = depth if is_branch_root else branch_root_depth

        for target in targets:
            if target not in nodes_by_id:
                continue
            child_depth = depth + 1
            if new_root_depth is not None and child_depth - new_root_depth > BRANCH_INLINE_DEPTH:
                continue
            frontier.append((target, child_depth, new_root_depth))

    return steps


__all__ = ["MAX_FLOW_STEPS", "BRANCH_INLINE_DEPTH", "trace_flow"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_graph_flows_trace.py -v`

Expected: all 6 tests PASS.

- [ ] **Step 5: Lint**

Run: `ruff check agent/graph_analyzer/flows.py tests/test_graph_flows_trace.py`

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add agent/graph_analyzer/flows.py tests/test_graph_flows_trace.py
git commit -m "feat(graph): forward-trace with branch+cycle handling for flow derivation"
```

---

## Task 5 — Terminal classification

**Files:**
- Modify: `agent/graph_analyzer/flows.py` (add `classify_terminal`)
- Test: `tests/test_graph_flows_trace.py` (extend with terminal tests)

Spec §3 step 2: a flow terminates at response return / queue publish / external HTTP / DB write. For Phase 1, classify by the *last* step's node attributes and its outgoing edges:

- `queue_publish`: outgoing edge of `kind="calls"` whose target label matches `enqueue|publish|send_task|delay|apply_async`.
- `external_http`: outgoing edge whose target label matches `requests.(get|post|put|delete)|httpx.(get|post)|fetch|axios`.
- `db_write`: outgoing edge whose target label matches `session.add|session.commit|execute.*INSERT|execute.*UPDATE|execute.*DELETE`.
- `response`: outgoing edge to a return/response node OR no outgoing call edges in an HTTP-entered flow.
- `none`: no terminal-shape match — used when the trace stopped from depth cap or branch cap.

The classification is deliberately lossy in Phase 1 (it's a viewer hint). Wrong classifications don't break the flow blob; they only mis-label the terminal icon in the future Phase 3 UI.

- [ ] **Step 1: Add failing tests**

Append to `tests/test_graph_flows_trace.py`:

```python
from agent.graph_analyzer.flows import classify_terminal


def _terminal_blob(last_node_label_targets: list[tuple[str, str]]) -> RepoGraphBlob:
    """Build a blob where the final node ``last`` has the given outgoing
    call edges, each to a node whose ``label`` is the second tuple item.
    """
    nodes = [_fn("a"), _fn("last")]
    edges = [_call("a", "last")]
    for target_id, target_label in last_node_label_targets:
        nodes.append(
            Node(
                id=target_id, kind="function", label=target_label,
                file="x.py", area="src",
            ),
        )
        edges.append(_call("last", target_id))
    return _blob(nodes, edges)


def test_terminal_queue_publish():
    blob = _terminal_blob([("q.enqueue", "enqueue")])
    kind = classify_terminal(blob, last_step_node_id="last", entry_kind="http")
    assert kind == "queue_publish"


def test_terminal_external_http():
    blob = _terminal_blob([("h.get", "requests.get")])
    kind = classify_terminal(blob, last_step_node_id="last", entry_kind="http")
    assert kind == "external_http"


def test_terminal_db_write():
    blob = _terminal_blob([("s.commit", "session.commit")])
    kind = classify_terminal(blob, last_step_node_id="last", entry_kind="http")
    assert kind == "db_write"


def test_terminal_response_for_http_with_no_outgoing():
    blob = _terminal_blob([])
    kind = classify_terminal(blob, last_step_node_id="last", entry_kind="http")
    assert kind == "response"


def test_terminal_none_when_unclassifiable_for_queue_entry():
    blob = _terminal_blob([])
    kind = classify_terminal(blob, last_step_node_id="last", entry_kind="queue")
    assert kind == "none"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_graph_flows_trace.py -v`

Expected: 5 new tests FAIL with ImportError on `classify_terminal`.

- [ ] **Step 3: Implement `classify_terminal`**

Append to `agent/graph_analyzer/flows.py`, alongside `trace_flow`:

```python
import re

from shared.types import EntryPointKind, TerminalKind

_QUEUE_PUBLISH_RE = re.compile(
    r"^(?:enqueue|publish|send_task|delay|apply_async)$",
)
_EXTERNAL_HTTP_RE = re.compile(
    r"^(?:requests\.(?:get|post|put|delete|patch)|httpx\.(?:get|post|put|delete|patch)|fetch|axios(?:\.\w+)?)$",
)
_DB_WRITE_RE = re.compile(
    r"^(?:session\.(?:add|delete|commit|merge)|.*INSERT.*|.*UPDATE.*|.*DELETE.*)$",
)


def classify_terminal(
    blob: RepoGraphBlob,
    last_step_node_id: str,
    entry_kind: EntryPointKind,
) -> TerminalKind:
    """Classify the terminal kind for a flow whose trace ends at *last_step_node_id*.

    Looks at the outgoing call edges of the last step's node. If any
    match a queue/http/db pattern (in that precedence), returns the
    matching kind. Otherwise: HTTP-entered flows with no outgoing call
    edges default to ``"response"``; other entry kinds default to
    ``"none"``.
    """
    nodes_by_id = {n.id: n for n in blob.nodes}
    outgoing_targets: list[Node] = []
    for edge in blob.edges:
        if edge.kind == "calls" and edge.source == last_step_node_id:
            target = nodes_by_id.get(edge.target)
            if target is not None:
                outgoing_targets.append(target)

    for target in outgoing_targets:
        if _QUEUE_PUBLISH_RE.match(target.label):
            return "queue_publish"
    for target in outgoing_targets:
        if _EXTERNAL_HTTP_RE.match(target.label):
            return "external_http"
    for target in outgoing_targets:
        if _DB_WRITE_RE.match(target.label):
            return "db_write"

    if not outgoing_targets and entry_kind == "http":
        return "response"
    return "none"


__all__ = [
    "MAX_FLOW_STEPS",
    "BRANCH_INLINE_DEPTH",
    "trace_flow",
    "classify_terminal",
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_graph_flows_trace.py -v`

Expected: all 11 tests PASS (6 trace + 5 terminal).

- [ ] **Step 5: Lint**

Run: `ruff check agent/graph_analyzer/flows.py tests/test_graph_flows_trace.py`

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add agent/graph_analyzer/flows.py tests/test_graph_flows_trace.py
git commit -m "feat(graph): terminal classification (response/queue/http/db) for flows"
```

---

## Task 6 — Top-level `derive_flow_blob`

**Files:**
- Modify: `agent/graph_analyzer/flows.py` (add `derive_flow_blob`)
- Test: `tests/test_graph_flows_derive.py`

Composes detection + trace + terminal + file-set hashing into a single `derive_flow_blob(blob: RepoGraphBlob, workspace_root: Path | None = None) -> FlowJsonBlob`.

- Stable flow IDs: `sha256(entry_point_node_id)[:12]`. Phase 1 deterministic, no name collisions.
- `file_set`: sorted unique file paths covered by the flow's steps.
- `file_set_hash`: SHA-256 over the concatenation of the file contents (read from `workspace_root`) in the file_set's sorted order, prefixed by each file's path + length. If `workspace_root` is None or a file is missing on disk, hash uses path-only fallback (the hash still changes when paths change but won't detect content edits — that's acceptable for Phase 1 since labelling lives in Phase 2 and will re-do it with a workspace).
- Single capability `{id: "unlabeled", flow_ids: [all], flow_membership_hash: sha256(sorted(flow_ids))}`.
- `unreached`: every node id in `blob.nodes` minus the union of step node ids across all flows.

- [ ] **Step 1: Write the failing test**

Create `tests/test_graph_flows_derive.py`:

```python
"""Top-level derive_flow_blob composes detection + trace + terminal +
hashing + capability assembly into a FlowJsonBlob.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from agent.graph_analyzer.flows import derive_flow_blob
from shared.types import (
    Edge,
    EdgeEvidence,
    Node,
    RepoGraphBlob,
)


def _blob():
    nodes = [
        Node(id="api/login.py::login", kind="function", label="login",
             file="api/login.py", area="api"),
        Node(id="api/login.py::validate", kind="function", label="validate",
             file="api/login.py", area="api"),
        Node(id="lib/db.py::session.commit", kind="function",
             label="session.commit", file="lib/db.py", area="lib"),
        # Web caller — produces the http edge that marks `login` as an entry.
        Node(id="web/login.tsx::handleSubmit", kind="function",
             label="handleSubmit", file="web/login.tsx", area="web"),
        # Unreached node — no edges in or out.
        Node(id="lib/orphan.py::unused", kind="function", label="unused",
             file="lib/orphan.py", area="lib"),
    ]
    edges = [
        Edge(source="web/login.tsx::handleSubmit",
             target="api/login.py::login", kind="http",
             evidence=EdgeEvidence(file="web/login.tsx", line=1, snippet="fetch"),
             source_kind="ast"),
        Edge(source="api/login.py::login", target="api/login.py::validate",
             kind="calls",
             evidence=EdgeEvidence(file="api/login.py", line=2, snippet="validate()"),
             source_kind="ast"),
        Edge(source="api/login.py::validate",
             target="lib/db.py::session.commit", kind="calls",
             evidence=EdgeEvidence(file="api/login.py", line=3, snippet="commit"),
             source_kind="ast"),
    ]
    return RepoGraphBlob(
        commit_sha="abc123",
        generated_at=datetime.now(tz=timezone.utc),
        analyser_version="test",
        areas=[],
        nodes=nodes,
        edges=edges,
    )


def test_derive_produces_single_flow_from_http_entry():
    blob = derive_flow_blob(_blob(), workspace_root=None)
    assert len(blob.flows) == 1
    flow = blob.flows[0]
    assert flow.entry_point.kind == "http"
    assert flow.entry_point.node_id == "api/login.py::login"
    assert [s.node_id for s in flow.steps] == [
        "api/login.py::login",
        "api/login.py::validate",
        "lib/db.py::session.commit",
    ]
    assert flow.terminal_kind == "db_write"


def test_flow_id_is_deterministic_hash_of_entry():
    expected = hashlib.sha256(b"api/login.py::login").hexdigest()[:12]
    blob = derive_flow_blob(_blob(), workspace_root=None)
    assert blob.flows[0].id == expected


def test_file_set_is_sorted_and_unique():
    blob = derive_flow_blob(_blob(), workspace_root=None)
    flow = blob.flows[0]
    assert flow.file_set == ["api/login.py", "lib/db.py"]


def test_capability_unlabeled_contains_all_flows():
    blob = derive_flow_blob(_blob(), workspace_root=None)
    assert len(blob.capabilities) == 1
    cap = blob.capabilities[0]
    assert cap.id == "unlabeled"
    assert cap.flow_ids == [f.id for f in blob.flows]
    expected_hash = hashlib.sha256(
        ",".join(sorted(cap.flow_ids)).encode("utf-8"),
    ).hexdigest()
    assert cap.flow_membership_hash == f"sha256:{expected_hash}"


def test_unreached_contains_orphan_node():
    blob = derive_flow_blob(_blob(), workspace_root=None)
    assert "lib/orphan.py::unused" in blob.unreached
    # Step nodes must NOT appear in unreached.
    for flow in blob.flows:
        for step in flow.steps:
            assert step.node_id not in blob.unreached


def test_file_set_hash_uses_workspace_contents_when_provided(tmp_path: Path):
    # Materialise files matching the blob's references.
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "login.py").write_text("def login(): pass\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "db.py").write_text("session = None\n")
    blob = derive_flow_blob(_blob(), workspace_root=tmp_path)
    flow = blob.flows[0]
    # File-content version of the hash differs from the path-only fallback.
    assert flow.file_set_hash.startswith("sha256:")

    other = derive_flow_blob(_blob(), workspace_root=None)
    assert flow.file_set_hash != other.flows[0].file_set_hash


def test_derived_at_commit_matches_blob_sha():
    blob = derive_flow_blob(_blob(), workspace_root=None)
    assert blob.derived_at_commit == "abc123"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_graph_flows_derive.py -v`

Expected: ImportError on `derive_flow_blob`.

- [ ] **Step 3: Implement `derive_flow_blob`**

Append to `agent/graph_analyzer/flows.py`:

```python
import hashlib
from pathlib import Path

from agent.graph_analyzer.entry_points import detect_entry_points
from shared.types import (
    Capability,
    Flow,
    FlowJsonBlob,
)

# Bumped when the derivation logic changes in a way that invalidates
# persisted flow_json blobs.
DERIVER_VERSION = "phase1"


def _stable_flow_id(entry_node_id: str) -> str:
    digest = hashlib.sha256(entry_node_id.encode("utf-8")).hexdigest()
    return digest[:12]


def _hash_file_set(file_set: list[str], workspace_root: Path | None) -> str:
    hasher = hashlib.sha256()
    for path in file_set:
        hasher.update(path.encode("utf-8"))
        hasher.update(b"\0")
        if workspace_root is not None:
            full = workspace_root / path
            try:
                data = full.read_bytes()
            except OSError:
                data = b""
            hasher.update(len(data).to_bytes(8, "big"))
            hasher.update(data)
        # When workspace_root is None we hash path-only — useful for unit
        # tests and as a deterministic fallback when no workspace is
        # available.  Phase 2 always supplies a workspace.
    return f"sha256:{hasher.hexdigest()}"


def _hash_flow_membership(flow_ids: list[str]) -> str:
    joined = ",".join(sorted(flow_ids))
    return f"sha256:{hashlib.sha256(joined.encode('utf-8')).hexdigest()}"


def derive_flow_blob(
    graph_blob: RepoGraphBlob,
    workspace_root: Path | None,
) -> FlowJsonBlob:
    """Compose entry-point detection + per-entry forward trace +
    terminal classification + file-set hashing into a single
    :class:`FlowJsonBlob`.

    When *workspace_root* is provided, ``file_set_hash`` uses the live
    contents of each file in the flow's ``file_set``. Otherwise the
    hash is path-only (Phase 1 callers may run without a workspace;
    Phase 2's labelling step always supplies one).
    """
    nodes_by_id = {n.id: n for n in graph_blob.nodes}
    entry_points = detect_entry_points(graph_blob)

    flows: list[Flow] = []
    reached: set[str] = set()
    for ep in entry_points:
        steps = trace_flow(graph_blob, ep)
        if not steps:
            continue
        for step in steps:
            reached.add(step.node_id)

        file_set = sorted(
            {nodes_by_id[s.node_id].file
             for s in steps
             if s.node_id in nodes_by_id and nodes_by_id[s.node_id].file},
        )
        last_step = steps[-1]
        terminal_kind = classify_terminal(
            graph_blob, last_step.node_id, ep.kind,
        )
        flow = Flow(
            id=_stable_flow_id(ep.node_id),
            entry_point=ep,
            terminal_node_id=last_step.node_id,
            terminal_kind=terminal_kind,
            steps=steps,
            file_set=file_set,
            file_set_hash=_hash_file_set(file_set, workspace_root),
            name=None,
            description=None,
        )
        flows.append(flow)

    flow_ids = [f.id for f in flows]
    capability = Capability(
        id="unlabeled",
        flow_ids=flow_ids,
        flow_membership_hash=_hash_flow_membership(flow_ids),
        name=None,
        description=None,
    )

    unreached = sorted(
        n.id for n in graph_blob.nodes
        if n.id not in reached and n.kind == "function"
    )

    return FlowJsonBlob(
        capabilities=[capability],
        flows=flows,
        unreached=unreached,
        derived_at_commit=graph_blob.commit_sha,
        deriver_version=DERIVER_VERSION,
    )


__all__ = [
    "MAX_FLOW_STEPS",
    "BRANCH_INLINE_DEPTH",
    "DERIVER_VERSION",
    "trace_flow",
    "classify_terminal",
    "derive_flow_blob",
]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_graph_flows_derive.py -v`

Expected: all 7 tests PASS.

- [ ] **Step 5: Lint**

Run: `ruff check agent/graph_analyzer/flows.py tests/test_graph_flows_derive.py`

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add agent/graph_analyzer/flows.py tests/test_graph_flows_derive.py
git commit -m "feat(graph): derive_flow_blob — compose entry-points + trace + terminal + hashing"
```

---

## Task 7 — Recompute endpoint

**Files:**
- Modify: `orchestrator/router.py` (add new endpoint near the existing graph endpoints around line 3415)
- Test: `tests/test_repo_graph_flows_endpoint.py`

Endpoint spec:

- **Path:** `POST /api/repos/{repo_id}/graph/flows/recompute`
- **Auth:** same as existing `POST /api/repos/{repo_id}/graph/refresh` — org-scoped via the same dependency.
- **Behaviour:**
  1. Load the latest completed `RepoGraph` row for the repo (the row where `is_complete=true`, ordered by `generated_at` desc).
  2. If no completed row exists, return 404 with a body explaining the user must run graph refresh first.
  3. Parse `graph_json` → `RepoGraphBlob`.
  4. Resolve the analyser workspace path (existing helper: `agent.graph_workspace.graph_workspace_path`).
  5. Call `derive_flow_blob(blob, workspace_root=workspace_path)`.
  6. Write the serialised `FlowJsonBlob` back to that row's `flow_json` column.
  7. Return 200 with `{repo_id, flow_count, capability_count, unreached_count, derived_at_commit}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_repo_graph_flows_endpoint.py`:

```python
"""POST /api/repos/{repo_id}/graph/flows/recompute (Phase 1)."""
from __future__ import annotations

import pytest

# Reuse the project's standard fixtures for org/repo/auth and a
# pre-populated repo_graph row. Existing tests show the pattern:
#   tests/test_repo_graph_refresh_endpoint.py
#   tests/test_repo_graph_api.py
# Copy the fixture wiring from whichever is closest to a clean baseline.


@pytest.mark.asyncio
async def test_recompute_writes_flow_json_for_completed_graph(
    api_client, seeded_repo_with_completed_graph,
):
    repo_id = seeded_repo_with_completed_graph.repo_id
    resp = await api_client.post(
        f"/api/repos/{repo_id}/graph/flows/recompute",
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["repo_id"] == repo_id
    assert data["flow_count"] >= 1
    assert data["capability_count"] == 1  # Phase 1 always has one
    assert data["derived_at_commit"] == seeded_repo_with_completed_graph.commit_sha


@pytest.mark.asyncio
async def test_recompute_404_when_no_completed_graph(
    api_client, seeded_repo_with_no_graph,
):
    repo_id = seeded_repo_with_no_graph.repo_id
    resp = await api_client.post(
        f"/api/repos/{repo_id}/graph/flows/recompute",
    )
    assert resp.status_code == 404
    assert "graph" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_recompute_403_for_other_org_repo(
    api_client, other_org_repo_with_graph,
):
    repo_id = other_org_repo_with_graph.repo_id
    resp = await api_client.post(
        f"/api/repos/{repo_id}/graph/flows/recompute",
    )
    assert resp.status_code in (403, 404)  # match existing graph endpoint convention
```

(If `seeded_repo_with_completed_graph` and `other_org_repo_with_graph` fixtures don't exist yet, copy the closest analogues from `tests/test_repo_graph_refresh_endpoint.py` and adapt — keep the fixture additions in `tests/conftest.py` or a local `conftest.py` next to this test, matching whichever pattern that file uses.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_repo_graph_flows_endpoint.py -v`

Expected: 404 from the not-yet-existing route, or fixture errors. The endpoint does not exist.

- [ ] **Step 3: Implement the endpoint**

Open `orchestrator/router.py`, find the existing `POST /api/repos/{repo_id}/graph/refresh` route (around line 3415). Add immediately after its handler:

```python
@router.post(
    "/repos/{repo_id}/graph/flows/recompute",
    status_code=200,
)
async def recompute_repo_graph_flows(
    repo_id: int,
    org: Organization = Depends(require_current_org),  # match existing dependency
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    """Re-derive the capability/flow map from the latest completed
    RepoGraph row for *repo_id*. Writes the resulting blob to
    `RepoGraph.flow_json`. Phase 1: every flow lands in a single
    'unlabeled' capability (Phase 2 will name them via LLM).

    See docs/superpowers/specs/2026-05-22-repo-graph-capability-flow-map-design.md
    """
    # Reuse the existing org+repo ownership check used by /graph/refresh.
    repo = await _load_org_owned_repo(session, repo_id, org.id)
    if repo is None:
        raise HTTPException(status_code=404, detail="Repo not found")

    row = (
        await session.execute(
            select(RepoGraph)
            .where(RepoGraph.repo_id == repo_id, RepoGraph.is_complete.is_(True))
            .order_by(RepoGraph.generated_at.desc())
            .limit(1),
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "No completed graph analysis for this repo yet. "
                "Run POST /graph/refresh first."
            ),
        )

    graph_blob = RepoGraphBlob.model_validate(row.graph_json)
    workspace_path = graph_workspace_path(repo_id)
    flow_blob = derive_flow_blob(
        graph_blob,
        workspace_root=workspace_path if workspace_path.exists() else None,
    )
    row.flow_json = flow_blob.model_dump(mode="json")
    await session.commit()

    return {
        "repo_id": repo_id,
        "flow_count": len(flow_blob.flows),
        "capability_count": len(flow_blob.capabilities),
        "unreached_count": len(flow_blob.unreached),
        "derived_at_commit": flow_blob.derived_at_commit,
    }
```

Add the necessary imports near the top of the existing import block (or wherever the file groups graph-related imports):

```python
from agent.graph_analyzer.flows import derive_flow_blob
from agent.graph_workspace import graph_workspace_path
from shared.types import RepoGraphBlob
```

(If `_load_org_owned_repo` is not the exact helper name used by `/graph/refresh`, use whatever helper that route uses — find the convention in the same file.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_repo_graph_flows_endpoint.py -v`

Expected: all 3 tests PASS.

- [ ] **Step 5: Run the full graph test suite to confirm no regression**

Run: `.venv/bin/python3 -m pytest tests/ -k "graph" -q`

Expected: same passing count as before this task, plus the new tests.

- [ ] **Step 6: Lint**

Run: `ruff check orchestrator/router.py tests/test_repo_graph_flows_endpoint.py`

Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add orchestrator/router.py tests/test_repo_graph_flows_endpoint.py
git commit -m "feat(graph): POST /graph/flows/recompute endpoint (Phase 1)"
```

---

## Task 8 — Agent op `which_capability`

**Files:**
- Modify: `agent/tools/query_repo_graph.py`
- Test: `tests/test_query_repo_graph_which_capability.py`

The op accepts `{"node": "<node_id>"}` and returns one of:

- `{"flows": [...], "capability": {...}}` — the node appears in at least one flow.
- `{"flows": [], "capability": null, "unreached": true}` — the node is in the graph but not on any flow.
- `{"error": "node_not_found"}` — the node id isn't in the graph at all.

In Phase 1, `capability.name` and `flow.name` are always `None`; the op still returns them honestly. The system-prompt nudge (deferred to Phase 2) will guide the agent on how to use the data even while names are absent.

- [ ] **Step 1: Write the failing test**

Create `tests/test_query_repo_graph_which_capability.py`:

```python
"""Tests for the `which_capability` op on the query_repo_graph tool."""
from __future__ import annotations

import json

import pytest

from agent.tools.query_repo_graph import QueryRepoGraphTool


@pytest.mark.asyncio
async def test_which_capability_returns_flow_for_reached_node(
    seeded_repo_with_flow_json,
):
    tool = QueryRepoGraphTool()
    ctx = seeded_repo_with_flow_json.tool_context
    result = await tool.run(
        ctx,
        {
            "repo_id": seeded_repo_with_flow_json.repo_id,
            "op": "which_capability",
            "params": {"node": seeded_repo_with_flow_json.reached_node_id},
        },
    )
    body = json.loads(result.output)
    assert body["op"] == "which_capability"
    assert body["result"]["capability"] is not None
    assert body["result"]["capability"]["id"] == "unlabeled"
    assert body["result"]["capability"]["name"] is None
    assert len(body["result"]["flows"]) >= 1
    assert body["result"].get("unreached") is not True


@pytest.mark.asyncio
async def test_which_capability_reports_unreached(
    seeded_repo_with_flow_json,
):
    tool = QueryRepoGraphTool()
    ctx = seeded_repo_with_flow_json.tool_context
    result = await tool.run(
        ctx,
        {
            "repo_id": seeded_repo_with_flow_json.repo_id,
            "op": "which_capability",
            "params": {"node": seeded_repo_with_flow_json.unreached_node_id},
        },
    )
    body = json.loads(result.output)
    assert body["result"]["unreached"] is True
    assert body["result"]["flows"] == []
    assert body["result"]["capability"] is None


@pytest.mark.asyncio
async def test_which_capability_node_not_in_graph(
    seeded_repo_with_flow_json,
):
    tool = QueryRepoGraphTool()
    ctx = seeded_repo_with_flow_json.tool_context
    result = await tool.run(
        ctx,
        {
            "repo_id": seeded_repo_with_flow_json.repo_id,
            "op": "which_capability",
            "params": {"node": "nonexistent::xyz"},
        },
    )
    body = json.loads(result.output)
    assert body["result"]["error"] == "node_not_found"
```

(Fixture `seeded_repo_with_flow_json` should mirror the pattern of fixtures used by `tests/test_query_repo_graph_tool.py`. Build it in `tests/conftest.py` or a local conftest by populating a `RepoGraph` row with `graph_json` AND `flow_json` — derive `flow_json` via `derive_flow_blob` on the seed graph.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_query_repo_graph_which_capability.py -v`

Expected: the tool rejects `op="which_capability"` because it's not in `_KNOWN_OPS`.

- [ ] **Step 3: Wire up the op**

Edit `agent/tools/query_repo_graph.py`:

1. Add `"which_capability"` to `_KNOWN_OPS`:

```python
_KNOWN_OPS: frozenset[str] = frozenset(
    {
        "callers_of",
        "callees_of",
        "outgoing_edges",
        "incoming_edges",
        "public_surface",
        "path_between",
        "violates_boundaries",
        "which_capability",
    },
)
```

2. Add the dispatch case wherever the tool maps `op` → handler. Implement the handler as:

```python
def _which_capability(
    flow_json: dict | None,
    graph_blob: RepoGraphBlob,
    params: dict,
) -> dict:
    node_id = params.get("node")
    if not node_id:
        return {"error": "missing_param:node"}
    if node_id not in {n.id for n in graph_blob.nodes}:
        return {"error": "node_not_found"}
    if flow_json is None:
        return {
            "flows": [],
            "capability": None,
            "unreached": True,
            "note": "flow_json not computed yet — POST /graph/flows/recompute",
        }
    blob = FlowJsonBlob.model_validate(flow_json)
    matching_flows = [
        {"id": f.id, "name": f.name, "entry_point_node_id": f.entry_point.node_id,
         "terminal_kind": f.terminal_kind}
        for f in blob.flows
        if any(s.node_id == node_id for s in f.steps)
    ]
    if not matching_flows:
        return {
            "flows": [],
            "capability": None,
            "unreached": True,
        }
    # Phase 1: only one capability covers everything.
    matching_cap = next(
        (
            {"id": c.id, "name": c.name, "description": c.description}
            for c in blob.capabilities
            if any(fid in c.flow_ids for fid in (f["id"] for f in matching_flows))
        ),
        None,
    )
    return {"flows": matching_flows, "capability": matching_cap, "unreached": False}
```

3. In the main `run` method, read both `graph_json` and `flow_json` from the `RepoGraph` row and pass them to the dispatcher when `op == "which_capability"`. The other ops continue to consume only the `RepoGraphBlob`.

Add the import:

```python
from shared.types import FlowJsonBlob
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_query_repo_graph_which_capability.py -v`

Expected: all 3 tests PASS.

- [ ] **Step 5: Run the existing query_repo_graph test suite to confirm no regression**

Run: `.venv/bin/python3 -m pytest tests/test_query_repo_graph_tool.py -v`

Expected: no new failures.

- [ ] **Step 6: Lint**

Run: `ruff check agent/tools/query_repo_graph.py tests/test_query_repo_graph_which_capability.py`

Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add agent/tools/query_repo_graph.py tests/test_query_repo_graph_which_capability.py
git commit -m "feat(graph): which_capability op on query_repo_graph (Phase 1)"
```

---

## Task 9 — End-to-end smoke against an existing fixture

**Files:**
- Create: `tests/test_graph_flows_e2e.py`

Run the full pipeline against the existing `tests/fixtures/graph_repo_python/` fixture to confirm the derivation produces a sane blob end-to-end. This test is the cheapest regression guard for "did I break something across the modules I just wrote."

- [ ] **Step 1: Write the test**

Create `tests/test_graph_flows_e2e.py`:

```python
"""End-to-end: run the existing graph pipeline on the python fixture,
then derive flows, and assert the result has the shape we expect.

This is a smoke test, not a comprehensive correctness test — the
per-module unit tests in test_graph_entry_points / test_graph_flows_*
cover behaviour. This one catches integration drift.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.graph_analyzer import pipeline
from agent.graph_analyzer.flows import derive_flow_blob


@pytest.mark.asyncio
async def test_derive_against_python_fixture(tmp_path: Path):
    fixture_root = Path(__file__).parent / "fixtures" / "graph_repo_python"
    # Use whichever pipeline entry the existing graph tests use — see
    # tests/test_graph_pipeline.py for the canonical invocation.
    graph_blob = await pipeline.analyze_repo(
        workspace=fixture_root,
        commit_sha="0" * 40,
        analyser_version="test",
    )
    flow_blob = derive_flow_blob(graph_blob, workspace_root=fixture_root)

    # The fixture is small enough that any blob with > 0 nodes should
    # produce either flows or unreached entries — never both empty.
    assert flow_blob.flows or flow_blob.unreached
    assert len(flow_blob.capabilities) == 1
    assert flow_blob.capabilities[0].id == "unlabeled"
    assert flow_blob.derived_at_commit == graph_blob.commit_sha
    assert flow_blob.deriver_version == "phase1"

    # Every node referenced by a flow's steps must exist in the graph.
    node_ids = {n.id for n in graph_blob.nodes}
    for flow in flow_blob.flows:
        for step in flow.steps:
            assert step.node_id in node_ids
```

(Inspect `tests/test_graph_pipeline.py` for the exact `analyze_repo` / `Pipeline` invocation that fits the project's current shape; use the same one. The point of this test is integration, so it should run against the same surface the rest of the graph tests use.)

- [ ] **Step 2: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_graph_flows_e2e.py -v`

Expected: PASS.

- [ ] **Step 3: Lint**

Run: `ruff check tests/test_graph_flows_e2e.py`

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add tests/test_graph_flows_e2e.py
git commit -m "test(graph): e2e smoke for flow derivation against python fixture"
```

---

## Task 10 — Final sweep: full suite + ruff format check

**Files:**
- None (verification only)

- [ ] **Step 1: Run the full unit test suite**

Run: `.venv/bin/python3 -m pytest tests/ -q`

Expected: all tests PASS except the known pre-existing failure in `tests/test_slack_multi_team_routing.py` (recorded in project memory as the only failing test on the baseline). No *new* failures from this work. If you see additional failures, bisect with `git diff` to find which task introduced them.

- [ ] **Step 2: Run ruff check on the whole project**

Run: `ruff check .`

Expected: no errors.

- [ ] **Step 3: Run ruff format check**

Run: `ruff format --check .`

Expected: no formatting violations. If any, run `ruff format .` and commit the formatting fix as a separate commit.

- [ ] **Step 4: If formatting fix needed, commit it**

```bash
ruff format .
git add -A
git commit -m "style: ruff format after Phase 1 of capability/flow map"
```

- [ ] **Step 5: Final inspection**

Run: `git diff main --stat` and confirm the file list matches the "File structure" block at the top of this plan. Anything extra is suspect; anything missing is a gap.

---

## What lands after Phase 1

Once this plan is merged:

- A repo with a completed graph analysis can hit `POST /api/repos/{repo_id}/graph/flows/recompute` and get a populated `flow_json` written.
- The agent can call `query_repo_graph(repo_id, "which_capability", {"node": "..."})` and see which flow(s) a function appears in (capability is always `"unlabeled"` until Phase 2).
- The Unreached tray's data is sitting in `flow_json.unreached` — ready for the Phase 3 UI to render.

Phases 2-5 (Phase 2 = LLM labelling with file-hash invalidation; Phase 3 = `map-canvas.tsx` LOD 0 + LOD 1 + tab bar; Phase 4 = LOD 2 step chains + LOD 3 source + boundary ports; Phase 5 = URL state, keyboard nav, polish) get their own plans once Phase 1 lands.
