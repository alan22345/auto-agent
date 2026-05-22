# Repo-Graph Capability / Flow Map — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add LLM labelling for flows + capabilities, with content-hash invalidation so unchanged surfaces don't trigger re-labelling. After Phase 2, the Map view (Phase 3+) sees human-readable names for capabilities ("Authentication", "Carbon Calc") and flows ("Google OAuth Login", "Submit Emissions Report") instead of `"unlabeled"` placeholders.

**Architecture:** A new module `agent/graph_analyzer/flow_labeler.py` provides one public async function `label_flow_blob(blob, prior_blob, workspace_root, provider)` that fills in the `name`/`description` fields. The Phase 1 derivation stays pure and unchanged; the recompute endpoint composes derivation → labelling → persistence. Per-flow cache: skip the LLM call when a flow's `file_set_hash` matches a prior label. Per-capability cache: skip when the emitted `flow_membership_hash` matches.

**Tech Stack:** Python 3.12, async, structlog, Pydantic. Reuses `agent.llm.structured.complete_json` + `agent.llm.get_structured_extractor_provider` (Haiku via Bedrock, fast + cheap). Same pattern `agent/graph_analyzer/gap_fill.py` uses today.

**Reference:** Spec §4 (LLM labelling with file-hash invalidation) and §3 step 5 (capability grouping). Phase 1 plan at `docs/superpowers/plans/2026-05-22-repo-graph-capability-flow-map-phase-1.md`.

**Phase 2 boundary discipline (deliberate handoffs to Phase 3):**

- No UI work. The labelled `FlowJsonBlob` flows out via the same endpoint and `which_capability` agent op.
- No re-architecting of `derive_flow_blob`. Phase 2 is additive — a separate `label_flow_blob` step composed after derivation.
- No batch/job infrastructure. Phase 2 calls the LLM synchronously inside the recompute endpoint. If the model rate-limits, the user retries.
- No background auto-refresh. Phase 1's "user clicks Recompute" model carries forward.

---

## File structure (final shape after Phase 2)

**New files:**

- `agent/graph_analyzer/flow_labeler.py` — public `label_flow_blob` + private helpers (`_load_file_slices`, `_label_flow`, `_label_capabilities`).
- `tests/test_graph_flow_labeler_flows.py` — per-flow labelling + file-hash cache tests.
- `tests/test_graph_flow_labeler_capabilities.py` — capability grouping + membership-hash cache tests.
- `tests/test_graph_flow_labeler_compose.py` — `label_flow_blob` end-to-end with mocked provider.
- `tests/test_repo_graph_flows_endpoint_phase2.py` — recompute endpoint integration with labelling.

**Modified files:**

- `shared/types.py` — extend `Flow` and `Capability` with `labeled_at_commit: str | None = None`; extend `FlowJsonBlob` with `labeler_model: str | None = None`.
- `orchestrator/router.py` — recompute endpoint reads prior `flow_json`, calls `label_flow_blob` after `derive_flow_blob`.
- `agent/graph_analyzer/flows.py` — no functional change, but `derive_flow_blob`'s docstring updated to note Phase 2 labelling is a separate step.

---

## Task 1 — Extend schema with Phase 2 label-tracking fields

**Files:**
- Modify: `shared/types.py` (`Flow`, `Capability`, `FlowJsonBlob`)
- Test: `tests/test_graph_flow_json_schema.py` (extend with new round-trip)

The three new fields are optional and default to `None`. Phase 1-written blobs deserialise without change. Phase 2 populates them on relabel.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_graph_flow_json_schema.py`:

```python
def test_flow_phase2_fields_default_to_none():
    """Phase 2 adds labeled_at_commit on Flow + Capability and
    labeler_model on FlowJsonBlob. All default to None so Phase 1 blobs
    continue to deserialise."""
    from shared.types import Capability, Flow, FlowJsonBlob

    flow = Flow(
        id="x",
        entry_point=EntryPoint(node_id="m.f", kind="http"),
        terminal_node_id="m.f",
        terminal_kind="response",
        steps=[FlowStep(node_id="m.f", depth=0)],
        file_set=[],
        file_set_hash="sha256:abc",
    )
    assert flow.labeled_at_commit is None

    cap = Capability(
        id="c",
        flow_ids=["x"],
        flow_membership_hash="sha256:def",
    )
    assert cap.labeled_at_commit is None

    blob = FlowJsonBlob(
        capabilities=[cap],
        flows=[flow],
        unreached=[],
        derived_at_commit="sha:0",
        deriver_version=DERIVER_VERSION,
    )
    assert blob.labeler_model is None


def test_flow_phase2_fields_round_trip_populated():
    """When labelled, the new fields survive a round trip."""
    from shared.types import Capability, Flow, FlowJsonBlob

    flow = Flow(
        id="x",
        entry_point=EntryPoint(node_id="m.f", kind="http"),
        terminal_node_id="m.f",
        terminal_kind="response",
        steps=[FlowStep(node_id="m.f", depth=0)],
        file_set=[],
        file_set_hash="sha256:abc",
        name="Login Flow",
        description="Authenticates the user via OAuth.",
        labeled_at_commit="sha:7e9f",
    )
    cap = Capability(
        id="c",
        flow_ids=["x"],
        flow_membership_hash="sha256:def",
        name="Authentication",
        description="User identity and sessions.",
        labeled_at_commit="sha:7e9f",
    )
    blob = FlowJsonBlob(
        capabilities=[cap],
        flows=[flow],
        unreached=[],
        derived_at_commit="sha:7e9f",
        deriver_version=DERIVER_VERSION,
        labeler_model="claude-haiku-4-5",
    )
    again = FlowJsonBlob.model_validate(blob.model_dump())
    assert again == blob
    assert again.flows[0].labeled_at_commit == "sha:7e9f"
    assert again.capabilities[0].labeled_at_commit == "sha:7e9f"
    assert again.labeler_model == "claude-haiku-4-5"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/Users/alanyeginchibayev/Documents/Github/auto-agent/.venv/bin/python3 -m pytest tests/test_graph_flow_json_schema.py -v`

Expected: two new tests fail with `AttributeError` (fields don't exist yet).

- [ ] **Step 3: Add the fields to `shared/types.py`**

Find the `Flow` class (it's in the "Capability / flow derivation (Phase 1...)" block) and add a new field after `description`:

```python
    labeled_at_commit: str | None = None
    """Commit SHA at which this flow's name+description were generated by
    the Phase 2 labeller. ``None`` until the first label. Reused on
    subsequent recomputes when ``file_set_hash`` matches the prior blob."""
```

Find the `Capability` class and add the same field after `description`:

```python
    labeled_at_commit: str | None = None
    """Commit SHA at which this capability's name+description were
    generated. ``None`` until the first label. Reused when
    ``flow_membership_hash`` matches the prior blob."""
```

Find `FlowJsonBlob` and add after `deriver_version`:

```python
    labeler_model: str | None = None
    """Identifier of the LLM model that produced the most recent labels
    (e.g. ``"claude-haiku-4-5"``). ``None`` if no labelling has happened
    yet (Phase 1 emits this as ``None``)."""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/alanyeginchibayev/Documents/Github/auto-agent/.venv/bin/python3 -m pytest tests/test_graph_flow_json_schema.py -v`

Expected: all tests in that file pass (8 total — the 6 from Phase 1 + the 2 new).

- [ ] **Step 5: Lint**

Run: `ruff check shared/types.py tests/test_graph_flow_json_schema.py`

Expected: no new errors.

- [ ] **Step 6: Commit**

```bash
git add shared/types.py tests/test_graph_flow_json_schema.py
git commit -m "feat(graph): Phase 2 label-tracking fields on Flow/Capability/FlowJsonBlob"
```

---

## Task 2 — File-slice loader helper

**Files:**
- Create: `agent/graph_analyzer/flow_labeler.py` (begin — `_load_file_slices` only)
- Test: `tests/test_graph_flow_labeler_flows.py` (begin)

To label a flow, the LLM needs a snippet of source: the entry point's function body + the terminal step's function body. Reading whole files is wasteful — we read only the lines covered by the flow's steps.

`_load_file_slices(workspace_root, steps, nodes_by_id, max_lines_per_step=40)` returns a list of `{file, lines, content}` records, one per step that has a `line_start`/`line_end` in its referenced Node. Lines are clamped to `max_lines_per_step`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_graph_flow_labeler_flows.py`:

```python
"""Tests for the flow-level labeller (Phase 2).

Covers _load_file_slices first; per-flow LLM labelling and the
file-hash cache come in subsequent tasks.
"""
from __future__ import annotations

from pathlib import Path

from agent.graph_analyzer.flow_labeler import _load_file_slices
from shared.types import FlowStep, Node


def _node(id: str, file: str, line_start: int, line_end: int) -> Node:
    return Node(
        id=id,
        kind="function",
        label=id.split("::")[-1],
        file=file,
        line_start=line_start,
        line_end=line_end,
        area="src",
    )


def test_load_slices_reads_lines_in_range(tmp_path: Path) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "login.py").write_text("\n".join(f"line {i}" for i in range(1, 21)) + "\n")

    nodes = {
        "api/login.py::login": _node("api/login.py::login", "api/login.py", 3, 7),
    }
    steps = [FlowStep(node_id="api/login.py::login", depth=0)]

    slices = _load_file_slices(tmp_path, steps, nodes)
    assert len(slices) == 1
    s = slices[0]
    assert s["file"] == "api/login.py"
    assert s["lines"] == [3, 7]
    # 5 lines (3,4,5,6,7) of content present.
    assert s["content"].count("\n") == 5
    assert "line 3" in s["content"]
    assert "line 7" in s["content"]
    assert "line 8" not in s["content"]


def test_load_slices_truncates_long_ranges(tmp_path: Path) -> None:
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "big.py").write_text("\n".join(f"line {i}" for i in range(1, 101)) + "\n")

    nodes = {
        "api/big.py::huge": _node("api/big.py::huge", "api/big.py", 1, 100),
    }
    steps = [FlowStep(node_id="api/big.py::huge", depth=0)]

    slices = _load_file_slices(tmp_path, steps, nodes, max_lines_per_step=40)
    assert len(slices) == 1
    # Truncated to 40 lines.
    assert slices[0]["content"].count("\n") == 40
    assert slices[0]["lines"] == [1, 40]


def test_load_slices_skips_nodes_without_file_or_line_info(tmp_path: Path) -> None:
    # Node has no file / no line_start — skipped.
    nodes = {
        "unknown::x": Node(
            id="unknown::x", kind="function", label="x", area="src",
        ),
    }
    steps = [FlowStep(node_id="unknown::x", depth=0)]
    slices = _load_file_slices(tmp_path, steps, nodes)
    assert slices == []


def test_load_slices_skips_missing_files(tmp_path: Path) -> None:
    nodes = {
        "missing.py::x": _node("missing.py::x", "missing.py", 1, 5),
    }
    steps = [FlowStep(node_id="missing.py::x", depth=0)]
    slices = _load_file_slices(tmp_path, steps, nodes)
    assert slices == []


def test_load_slices_deduplicates_same_file_line_pair(tmp_path: Path) -> None:
    """If two steps point at the same (file, line_start, line_end), only
    one slice is returned — the LLM doesn't need it twice."""
    (tmp_path / "x.py").write_text("a\nb\nc\nd\ne\n")
    nodes = {
        "x.py::a": _node("x.py::a", "x.py", 1, 3),
        "x.py::b": _node("x.py::b", "x.py", 1, 3),  # same span as a
    }
    steps = [
        FlowStep(node_id="x.py::a", depth=0),
        FlowStep(node_id="x.py::b", depth=1),
    ]
    slices = _load_file_slices(tmp_path, steps, nodes)
    assert len(slices) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/Users/alanyeginchibayev/Documents/Github/auto-agent/.venv/bin/python3 -m pytest tests/test_graph_flow_labeler_flows.py -v`

Expected: ImportError on `agent.graph_analyzer.flow_labeler._load_file_slices`.

- [ ] **Step 3: Create the module with `_load_file_slices`**

Create `agent/graph_analyzer/flow_labeler.py`:

```python
"""Phase 2 LLM labelling for flows and capabilities (spec §4).

Public entry point is :func:`label_flow_blob`. It composes:

  per-flow labelling   →  per-capability grouping + labelling
       ↓                          ↓
  cache by file_set_hash    cache by flow_membership_hash

The labelled :class:`shared.types.FlowJsonBlob` is returned; the caller
(the recompute endpoint) persists it. The labeller is async and uses
:func:`agent.llm.structured.complete_json` for one-shot JSON output.

Cost discipline:
* Per-flow LLM calls cap source slices at ``MAX_LINES_PER_STEP`` lines.
* Total per-flow prompt tokens are bounded by the slice cap × step count.
* Capability grouping is a single LLM call over all flow summaries.
* Reuses prior labels whose ``file_set_hash`` / ``flow_membership_hash``
  match the input blob — the cache key contract from Phase 1 §4.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from shared.types import FlowStep, Node

if TYPE_CHECKING:
    pass  # Future: LLMProvider typed import added in Task 3.

log = structlog.get_logger(__name__)

#: Maximum source-line span included in the LLM prompt for one step.
#: Functions longer than this are truncated head-only — the leading lines
#: tend to carry the signature + docstring + early returns, which is
#: enough signal for naming.
MAX_LINES_PER_STEP = 40


def _load_file_slices(
    workspace_root: Path,
    steps: list[FlowStep],
    nodes_by_id: dict[str, Node],
    *,
    max_lines_per_step: int = MAX_LINES_PER_STEP,
) -> list[dict[str, object]]:
    """Read source slices for each step in *steps* from *workspace_root*.

    Returns a list of records ``{"file", "lines": [start, end], "content"}``,
    one per unique ``(file, line_start, line_end)`` triple. Skips steps
    whose node has no file, no line range, or whose file doesn't exist
    on disk. Line ranges longer than ``max_lines_per_step`` are head-
    truncated.
    """
    seen: set[tuple[str, int, int]] = set()
    out: list[dict[str, object]] = []
    for step in steps:
        node = nodes_by_id.get(step.node_id)
        if node is None:
            continue
        if not node.file or node.line_start is None or node.line_end is None:
            continue
        key = (node.file, node.line_start, node.line_end)
        if key in seen:
            continue
        seen.add(key)

        file_path = workspace_root / node.file
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        lines = text.splitlines()
        # File lines are 1-indexed; slice is [start-1, end] (end inclusive).
        start_zero = max(0, node.line_start - 1)
        end_zero = min(len(lines), node.line_end)
        clipped_end = min(end_zero, start_zero + max_lines_per_step)
        content_lines = lines[start_zero:clipped_end]
        content = "\n".join(content_lines) + "\n"
        out.append(
            {
                "file": node.file,
                "lines": [node.line_start, node.line_start + len(content_lines) - 1],
                "content": content,
            },
        )
    return out


__all__ = ["MAX_LINES_PER_STEP", "_load_file_slices"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `/Users/alanyeginchibayev/Documents/Github/auto-agent/.venv/bin/python3 -m pytest tests/test_graph_flow_labeler_flows.py -v`

Expected: all 5 tests pass.

- [ ] **Step 5: Lint**

Run: `ruff check agent/graph_analyzer/flow_labeler.py tests/test_graph_flow_labeler_flows.py`

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add agent/graph_analyzer/flow_labeler.py tests/test_graph_flow_labeler_flows.py
git commit -m "feat(graph): file-slice loader for flow labelling (Phase 2)"
```

---

## Task 3 — Per-flow LLM labelling

**Files:**
- Modify: `agent/graph_analyzer/flow_labeler.py` (add `_label_flow`)
- Modify: `tests/test_graph_flow_labeler_flows.py` (extend)

`_label_flow(provider, flow, slices)` builds a structured prompt around the flow's entry point + terminal + step source slices and returns `{name: str, description: str}` via `complete_json`. The function is async because the provider is.

The prompt is small and tight:
- System: "Name this code flow with a short label (≤4 words) and a one-sentence description (≤25 words)."
- User: structured payload listing entry point, terminal kind, step labels, and slice content.

Output schema (validated by `complete_json`'s retry mechanism):
```json
{"name": "Google OAuth Login", "description": "User clicks 'Sign in with Google'…"}
```

If the LLM returns malformed JSON or empty strings, retry up to 2 times via `complete_json`. If still failing, log + return `(None, None)` so the flow stays unlabelled (better than fabricating).

- [ ] **Step 1: Add failing tests**

Append to `tests/test_graph_flow_labeler_flows.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.graph_analyzer.flow_labeler import _label_flow
from shared.types import EntryPoint, Flow, FlowStep


def _flow(
    id_: str = "auth_a1b2",
    entry: str = "api/login.py::login",
    kind: str = "http",
    terminal: str = "api/login.py::login",
) -> Flow:
    return Flow(
        id=id_,
        entry_point=EntryPoint(node_id=entry, kind=kind),
        terminal_node_id=terminal,
        terminal_kind="response",
        steps=[FlowStep(node_id=entry, depth=0)],
        file_set=["api/login.py"],
        file_set_hash="sha256:test",
    )


@pytest.mark.asyncio
async def test_label_flow_returns_name_and_description_from_llm():
    mock_provider = MagicMock()
    # complete_json is what _label_flow calls; patch it via the provider
    # mock by mocking the module-level helper in the test (next test).
    from agent.graph_analyzer import flow_labeler

    flow = _flow()
    slices = [{"file": "api/login.py", "lines": [1, 5], "content": "def login(): ..."}]

    async def fake_complete_json(*args, **kwargs):
        return {"name": "Login Flow", "description": "Authenticates the user."}

    monkeypatched = AsyncMock(side_effect=fake_complete_json)
    flow_labeler.complete_json = monkeypatched  # type: ignore[attr-defined]
    try:
        name, desc = await _label_flow(mock_provider, flow, slices)
    finally:
        # Restore — every test should restore module-state side effects.
        from agent.llm.structured import complete_json as real
        flow_labeler.complete_json = real  # type: ignore[attr-defined]
    assert name == "Login Flow"
    assert desc == "Authenticates the user."
    monkeypatched.assert_awaited_once()


@pytest.mark.asyncio
async def test_label_flow_returns_none_when_llm_fails():
    from agent.graph_analyzer import flow_labeler

    async def fake_complete_json(*args, **kwargs):
        raise ValueError("could not parse")

    flow_labeler.complete_json = AsyncMock(side_effect=fake_complete_json)  # type: ignore[attr-defined]
    try:
        name, desc = await _label_flow(MagicMock(), _flow(), [])
    finally:
        from agent.llm.structured import complete_json as real
        flow_labeler.complete_json = real  # type: ignore[attr-defined]
    assert name is None
    assert desc is None


@pytest.mark.asyncio
async def test_label_flow_rejects_empty_strings():
    from agent.graph_analyzer import flow_labeler

    async def fake_complete_json(*args, **kwargs):
        return {"name": "", "description": ""}

    flow_labeler.complete_json = AsyncMock(side_effect=fake_complete_json)  # type: ignore[attr-defined]
    try:
        name, desc = await _label_flow(MagicMock(), _flow(), [])
    finally:
        from agent.llm.structured import complete_json as real
        flow_labeler.complete_json = real  # type: ignore[attr-defined]
    assert name is None
    assert desc is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/alanyeginchibayev/Documents/Github/auto-agent/.venv/bin/python3 -m pytest tests/test_graph_flow_labeler_flows.py -v`

Expected: ImportError on `_label_flow`.

- [ ] **Step 3: Implement `_label_flow`**

Append to `agent/graph_analyzer/flow_labeler.py`:

```python
from agent.llm.structured import complete_json
from agent.llm.types import Message
from shared.types import Flow

if TYPE_CHECKING:
    from agent.llm.base import LLMProvider

#: Maximum tokens for a per-flow naming response. The output is tiny
#: (a name + one sentence), so a tight cap prevents the model from
#: padding with reasoning. Mirrors gap_fill.py's choice.
_FLOW_LABEL_MAX_TOKENS = 256

_FLOW_LABEL_SYSTEM = (
    "You name code flows for a developer-facing repo map. Each flow is a "
    "trace from an entry point (HTTP route, queue handler, CLI command, "
    "scheduled job) to a terminal side effect.\n\n"
    "Given the entry point, terminal kind, and source-code slices, "
    "return JSON exactly:\n"
    '{"name": "<≤4 words, Title Case>", '
    '"description": "<≤25 words, one sentence>"}\n\n'
    "The name should be product-language (what the user does), not "
    "function names. Bad: \"login function\". Good: \"Google OAuth Login\"."
)


async def _label_flow(
    provider: "LLMProvider",
    flow: Flow,
    slices: list[dict[str, object]],
) -> tuple[str | None, str | None]:
    """Ask the LLM to name *flow* given its source slices.

    Returns ``(name, description)``. Returns ``(None, None)`` if the LLM
    call fails (parse error, empty strings) — caller leaves the flow
    unlabelled rather than fabricating a name.
    """
    payload = {
        "entry_point": flow.entry_point.node_id,
        "entry_kind": flow.entry_point.kind,
        "terminal_kind": flow.terminal_kind,
        "step_labels": [s.node_id for s in flow.steps[:10]],
        "source_slices": slices,
    }
    user_msg = Message(role="user", content=str(payload))

    try:
        response = await complete_json(
            provider,
            messages=[user_msg],
            system=_FLOW_LABEL_SYSTEM,
            max_tokens=_FLOW_LABEL_MAX_TOKENS,
            temperature=0.0,
            retries=2,
        )
    except ValueError as exc:
        log.warning("flow_label.parse_failed", flow_id=flow.id, error=str(exc))
        return (None, None)

    name = response.get("name") or None
    description = response.get("description") or None
    if not name or not description:
        log.warning("flow_label.empty_response", flow_id=flow.id, response=response)
        return (None, None)
    return (name, description)


__all__ = ["MAX_LINES_PER_STEP", "_label_flow", "_load_file_slices"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/alanyeginchibayev/Documents/Github/auto-agent/.venv/bin/python3 -m pytest tests/test_graph_flow_labeler_flows.py -v`

Expected: all 8 tests pass (5 from Task 2 + 3 new).

- [ ] **Step 5: Lint**

Run: `ruff check agent/graph_analyzer/flow_labeler.py tests/test_graph_flow_labeler_flows.py`

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add agent/graph_analyzer/flow_labeler.py tests/test_graph_flow_labeler_flows.py
git commit -m "feat(graph): per-flow LLM labelling with fail-safe fallback"
```

---

## Task 4 — Capability grouping LLM call

**Files:**
- Modify: `agent/graph_analyzer/flow_labeler.py` (add `_label_capabilities`)
- Test: `tests/test_graph_flow_labeler_capabilities.py`

`_label_capabilities(provider, flows)` makes ONE structured LLM call. Input: the list of all flows with their (already-labelled) name + entry point + terminal_kind. Output: 5-12 capabilities, each with a name, description, and the `flow_ids` it contains.

The LLM is instructed to:
- Produce between 5 and 12 capabilities total.
- Each flow goes into exactly one capability.
- Flows that don't fit conceptually land in a single `"Other"` capability.

On failure: return `[]` — caller fall back to the single `"unlabeled"` capability containing every flow (Phase 1 shape).

- [ ] **Step 1: Write failing tests**

Create `tests/test_graph_flow_labeler_capabilities.py`:

```python
"""Tests for capability grouping + labelling (Phase 2)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.graph_analyzer.flow_labeler import _label_capabilities
from shared.types import EntryPoint, Flow, FlowStep


def _flow(id_: str, name: str | None) -> Flow:
    return Flow(
        id=id_,
        entry_point=EntryPoint(node_id=f"x/{id_}", kind="http"),
        terminal_node_id=f"x/{id_}",
        terminal_kind="response",
        steps=[FlowStep(node_id=f"x/{id_}", depth=0)],
        file_set=[f"x/{id_}.py"],
        file_set_hash=f"sha256:{id_}",
        name=name,
    )


@pytest.mark.asyncio
async def test_label_capabilities_groups_flows():
    from agent.graph_analyzer import flow_labeler

    flows = [
        _flow("a", "Google Login"),
        _flow("b", "GitHub Login"),
        _flow("c", "Submit Report"),
        _flow("d", "View Dashboard"),
    ]
    expected_response = {
        "capabilities": [
            {"name": "Auth", "description": "Sign-in flows.", "flow_ids": ["a", "b"]},
            {"name": "Reports", "description": "Report submission and viewing.",
             "flow_ids": ["c", "d"]},
        ],
    }

    flow_labeler.complete_json = AsyncMock(return_value=expected_response)  # type: ignore[attr-defined]
    try:
        caps = await _label_capabilities(MagicMock(), flows)
    finally:
        from agent.llm.structured import complete_json as real
        flow_labeler.complete_json = real  # type: ignore[attr-defined]

    assert len(caps) == 2
    assert caps[0]["name"] == "Auth"
    assert set(caps[0]["flow_ids"]) == {"a", "b"}
    assert caps[1]["name"] == "Reports"
    assert set(caps[1]["flow_ids"]) == {"c", "d"}


@pytest.mark.asyncio
async def test_label_capabilities_returns_empty_on_failure():
    from agent.graph_analyzer import flow_labeler

    flow_labeler.complete_json = AsyncMock(side_effect=ValueError("nope"))  # type: ignore[attr-defined]
    try:
        caps = await _label_capabilities(MagicMock(), [_flow("a", "Login")])
    finally:
        from agent.llm.structured import complete_json as real
        flow_labeler.complete_json = real  # type: ignore[attr-defined]

    assert caps == []


@pytest.mark.asyncio
async def test_label_capabilities_drops_capabilities_with_unknown_flow_ids():
    """If the LLM hallucinates a flow_id not in the input, the
    capability that references it is dropped — every flow_id in every
    output capability must exist in the input."""
    from agent.graph_analyzer import flow_labeler

    flows = [_flow("a", "Login"), _flow("b", "Other")]
    bad_response = {
        "capabilities": [
            {"name": "Auth", "description": "Sign-in.", "flow_ids": ["a", "ghost"]},
            {"name": "Other", "description": "Misc.", "flow_ids": ["b"]},
        ],
    }
    flow_labeler.complete_json = AsyncMock(return_value=bad_response)  # type: ignore[attr-defined]
    try:
        caps = await _label_capabilities(MagicMock(), flows)
    finally:
        from agent.llm.structured import complete_json as real
        flow_labeler.complete_json = real  # type: ignore[attr-defined]
    # The "Auth" capability is dropped because "ghost" isn't a real id.
    assert len(caps) == 1
    assert caps[0]["name"] == "Other"


@pytest.mark.asyncio
async def test_label_capabilities_handles_empty_flow_list():
    from agent.graph_analyzer import flow_labeler

    flow_labeler.complete_json = AsyncMock()  # type: ignore[attr-defined]
    try:
        caps = await _label_capabilities(MagicMock(), [])
    finally:
        from agent.llm.structured import complete_json as real
        flow_labeler.complete_json = real  # type: ignore[attr-defined]

    # No flows = no LLM call = no capabilities.
    flow_labeler.complete_json.assert_not_awaited()  # type: ignore[attr-defined]
    assert caps == []
```

- [ ] **Step 2: Run tests to verify failure**

Run: `/Users/alanyeginchibayev/Documents/Github/auto-agent/.venv/bin/python3 -m pytest tests/test_graph_flow_labeler_capabilities.py -v`

Expected: ImportError on `_label_capabilities`.

- [ ] **Step 3: Implement `_label_capabilities`**

Append to `agent/graph_analyzer/flow_labeler.py`:

```python
_CAPABILITY_LABEL_MAX_TOKENS = 1024

_CAPABILITY_LABEL_SYSTEM = (
    "You group code flows into named capabilities for a developer-facing "
    "repo map. A capability is a coherent set of user-visible behaviours "
    "(e.g. \"Authentication\", \"Carbon Calculation\").\n\n"
    "Given a list of flows (each with an id, a flow name, an entry point, "
    "and a terminal kind), return JSON exactly:\n"
    '{"capabilities": [\n'
    '  {"name": "<≤4 words, Title Case>",\n'
    '   "description": "<≤25 words, one sentence>",\n'
    '   "flow_ids": ["<id>", "<id>", ...]},\n'
    "  ...\n"
    "]}\n\n"
    "Rules:\n"
    "- Produce 5 to 12 capabilities total when possible. Fewer is fine if "
    "  the repo is small.\n"
    "- Each flow id appears in exactly one capability.\n"
    "- Flows that don't fit any group go into a single \"Other\" capability."
)


async def _label_capabilities(
    provider: "LLMProvider",
    flows: list[Flow],
) -> list[dict[str, object]]:
    """Ask the LLM to group *flows* into named capabilities.

    Returns a list of dicts ``{name, description, flow_ids}``. On LLM
    failure or empty input returns ``[]``; the caller falls back to the
    Phase 1 single-capability shape.

    Any returned capability that references flow_ids not in the input
    list is dropped (defends against hallucinated ids).
    """
    if not flows:
        return []

    payload = {
        "flows": [
            {
                "id": f.id,
                "name": f.name,
                "entry_point": f.entry_point.node_id,
                "entry_kind": f.entry_point.kind,
                "terminal_kind": f.terminal_kind,
            }
            for f in flows
        ],
    }
    user_msg = Message(role="user", content=str(payload))

    try:
        response = await complete_json(
            provider,
            messages=[user_msg],
            system=_CAPABILITY_LABEL_SYSTEM,
            max_tokens=_CAPABILITY_LABEL_MAX_TOKENS,
            temperature=0.0,
            retries=2,
        )
    except ValueError as exc:
        log.warning("capability_label.parse_failed", error=str(exc))
        return []

    raw_caps = response.get("capabilities") or []
    valid_ids = {f.id for f in flows}
    out: list[dict[str, object]] = []
    for cap in raw_caps:
        if not isinstance(cap, dict):
            continue
        flow_ids = cap.get("flow_ids", [])
        if not isinstance(flow_ids, list) or not all(fid in valid_ids for fid in flow_ids):
            log.warning(
                "capability_label.drop_unknown_flow_ids",
                cap_name=cap.get("name"),
                flow_ids=flow_ids,
            )
            continue
        if not cap.get("name") or not cap.get("description"):
            continue
        out.append(
            {
                "name": cap["name"],
                "description": cap["description"],
                "flow_ids": flow_ids,
            },
        )
    return out


__all__ = ["MAX_LINES_PER_STEP", "_label_capabilities", "_label_flow", "_load_file_slices"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/alanyeginchibayev/Documents/Github/auto-agent/.venv/bin/python3 -m pytest tests/test_graph_flow_labeler_capabilities.py -v`

Expected: all 4 tests pass.

- [ ] **Step 5: Lint**

Run: `ruff check agent/graph_analyzer/flow_labeler.py tests/test_graph_flow_labeler_capabilities.py`

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add agent/graph_analyzer/flow_labeler.py tests/test_graph_flow_labeler_capabilities.py
git commit -m "feat(graph): capability grouping LLM call with hallucination guard"
```

---

## Task 5 — Public `label_flow_blob` composition with caches

**Files:**
- Modify: `agent/graph_analyzer/flow_labeler.py` (add `label_flow_blob`)
- Test: `tests/test_graph_flow_labeler_compose.py`

`label_flow_blob(blob, prior_blob, workspace_root, provider, *, labeler_model="claude-haiku-4-5")` returns a new `FlowJsonBlob` with names + descriptions populated. The function:

1. Builds `nodes_by_id` lookup from the blob's reached nodes. (Note: `FlowJsonBlob` doesn't carry nodes — the caller must pass them, or we can reconstruct them from `RepoGraphBlob`. For Phase 2 we'll add a `nodes_by_id` parameter to keep the function pure and testable.)
2. For each flow in `blob.flows`:
   - **Cache lookup:** find the same id in `prior_blob.flows`. If found AND `file_set_hash` matches AND prior has a name: reuse prior name + description + `labeled_at_commit`.
   - **Cache miss:** load file slices via `_load_file_slices`, call `_label_flow`. Use `blob.derived_at_commit` as the new `labeled_at_commit`. Fall back to `None` if labelling fails.
3. Run `_label_capabilities` over the (now-labelled) flows. Returns a list of capability dicts.
4. For each emitted capability:
   - Compute `flow_membership_hash` over its `flow_ids`.
   - **Cache lookup:** find a capability in `prior_blob.capabilities` with the same hash. If found AND prior has a name: reuse prior name + description + `labeled_at_commit`.
   - **Cache miss:** use the LLM's emitted name + description, set `labeled_at_commit = blob.derived_at_commit`.
5. If `_label_capabilities` returns `[]` (LLM failed): fall back to Phase 1 shape — single `"unlabeled"` capability containing all flow_ids.
6. Return a new `FlowJsonBlob` with `labeler_model` set.

Signature:
```python
async def label_flow_blob(
    blob: FlowJsonBlob,
    prior_blob: FlowJsonBlob | None,
    workspace_root: Path,
    nodes_by_id: dict[str, Node],
    provider: "LLMProvider",
    *,
    labeler_model: str = "claude-haiku-4-5",
) -> FlowJsonBlob: ...
```

- [ ] **Step 1: Write failing tests**

Create `tests/test_graph_flow_labeler_compose.py`:

```python
"""Tests for label_flow_blob — the public Phase 2 entry point.

These tests mock the per-flow and per-capability LLM helpers and verify
the cache logic + cache-miss path.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.graph_analyzer.flow_labeler import label_flow_blob
from shared.types import (
    Capability,
    EntryPoint,
    Flow,
    FlowJsonBlob,
    FlowStep,
    Node,
)


def _node(node_id: str) -> Node:
    return Node(
        id=node_id, kind="function", label=node_id, file=f"{node_id}.py",
        area="src", line_start=1, line_end=3,
    )


def _flow(id_: str = "f1", entry: str = "x", file_hash: str = "h1") -> Flow:
    return Flow(
        id=id_,
        entry_point=EntryPoint(node_id=entry, kind="http"),
        terminal_node_id=entry,
        terminal_kind="response",
        steps=[FlowStep(node_id=entry, depth=0)],
        file_set=[f"{entry}.py"],
        file_set_hash=file_hash,
    )


def _blob(flows: list[Flow], commit: str = "sha:new") -> FlowJsonBlob:
    return FlowJsonBlob(
        capabilities=[],
        flows=flows,
        unreached=[],
        derived_at_commit=commit,
        deriver_version="phase1",
    )


@pytest.mark.asyncio
async def test_cache_hit_skips_per_flow_llm_call(tmp_path: Path):
    """When file_set_hash matches a prior labelled flow, no LLM call."""
    from agent.graph_analyzer import flow_labeler

    flow_label_mock = AsyncMock()
    cap_label_mock = AsyncMock(return_value=[
        {"name": "Auth", "description": "Sign-in.", "flow_ids": ["f1"]},
    ])

    prior = _blob(
        [Flow(
            id="f1",
            entry_point=EntryPoint(node_id="x", kind="http"),
            terminal_node_id="x",
            terminal_kind="response",
            steps=[FlowStep(node_id="x", depth=0)],
            file_set=["x.py"],
            file_set_hash="h1",
            name="Existing Name",
            description="Existing Desc.",
            labeled_at_commit="sha:old",
        )],
        commit="sha:old",
    )
    new_blob = _blob([_flow(id_="f1", entry="x", file_hash="h1")], commit="sha:new")

    flow_labeler._label_flow = flow_label_mock  # type: ignore[attr-defined]
    flow_labeler._label_capabilities = cap_label_mock  # type: ignore[attr-defined]
    try:
        result = await label_flow_blob(
            new_blob,
            prior_blob=prior,
            workspace_root=tmp_path,
            nodes_by_id={"x": _node("x")},
            provider=MagicMock(),
        )
    finally:
        # Restore real implementations.
        from agent.graph_analyzer.flow_labeler import _label_flow as real_lf
        from agent.graph_analyzer.flow_labeler import _label_capabilities as real_lc
        flow_labeler._label_flow = real_lf  # type: ignore[attr-defined]
        flow_labeler._label_capabilities = real_lc  # type: ignore[attr-defined]

    flow_label_mock.assert_not_awaited()  # cache hit, no LLM call
    assert result.flows[0].name == "Existing Name"
    assert result.flows[0].description == "Existing Desc."
    assert result.flows[0].labeled_at_commit == "sha:old"  # preserved


@pytest.mark.asyncio
async def test_cache_miss_calls_llm_and_sets_commit(tmp_path: Path):
    from agent.graph_analyzer import flow_labeler

    (tmp_path / "x.py").write_text("def x(): pass\n")

    flow_label_mock = AsyncMock(return_value=("Login Flow", "Authenticates."))
    cap_label_mock = AsyncMock(return_value=[
        {"name": "Auth", "description": "Sign-in.", "flow_ids": ["f1"]},
    ])

    # Prior blob has a flow but with a DIFFERENT file_set_hash — cache miss.
    prior = _blob(
        [Flow(
            id="f1",
            entry_point=EntryPoint(node_id="x", kind="http"),
            terminal_node_id="x",
            terminal_kind="response",
            steps=[FlowStep(node_id="x", depth=0)],
            file_set=["x.py"],
            file_set_hash="h_OLD",
            name="Old Name",
            description="Old Desc.",
            labeled_at_commit="sha:old",
        )],
        commit="sha:old",
    )
    new_blob = _blob([_flow(id_="f1", entry="x", file_hash="h_NEW")], commit="sha:new")

    flow_labeler._label_flow = flow_label_mock  # type: ignore[attr-defined]
    flow_labeler._label_capabilities = cap_label_mock  # type: ignore[attr-defined]
    try:
        result = await label_flow_blob(
            new_blob, prior_blob=prior, workspace_root=tmp_path,
            nodes_by_id={"x": _node("x")}, provider=MagicMock(),
        )
    finally:
        from agent.graph_analyzer.flow_labeler import _label_flow as real_lf
        from agent.graph_analyzer.flow_labeler import _label_capabilities as real_lc
        flow_labeler._label_flow = real_lf  # type: ignore[attr-defined]
        flow_labeler._label_capabilities = real_lc  # type: ignore[attr-defined]

    flow_label_mock.assert_awaited_once()
    assert result.flows[0].name == "Login Flow"
    assert result.flows[0].description == "Authenticates."
    assert result.flows[0].labeled_at_commit == "sha:new"


@pytest.mark.asyncio
async def test_capability_cache_hit_preserves_prior_name(tmp_path: Path):
    """When the emitted capability has the same flow_membership_hash as
    a prior capability, prior name + description are preserved."""
    from agent.graph_analyzer import flow_labeler

    # Prior had Auth capability with flow_ids ["f1", "f2"]; its hash:
    prior_hash = "sha256:" + hashlib.sha256(
        ",".join(sorted(["f1", "f2"])).encode("utf-8"),
    ).hexdigest()
    prior = FlowJsonBlob(
        flows=[
            Flow(id="f1", entry_point=EntryPoint(node_id="a", kind="http"),
                 terminal_node_id="a", terminal_kind="response",
                 steps=[FlowStep(node_id="a", depth=0)], file_set=[],
                 file_set_hash="h", name="A", description="a.",
                 labeled_at_commit="sha:old"),
            Flow(id="f2", entry_point=EntryPoint(node_id="b", kind="http"),
                 terminal_node_id="b", terminal_kind="response",
                 steps=[FlowStep(node_id="b", depth=0)], file_set=[],
                 file_set_hash="h", name="B", description="b.",
                 labeled_at_commit="sha:old"),
        ],
        capabilities=[
            Capability(id="cap_prior", flow_ids=["f1", "f2"],
                       flow_membership_hash=prior_hash,
                       name="Prior Auth", description="Old desc.",
                       labeled_at_commit="sha:old"),
        ],
        unreached=[], derived_at_commit="sha:old", deriver_version="phase1",
    )

    # New emit: same flow_ids → same hash → cache hit.
    flow_label_mock = AsyncMock(return_value=("X", "x."))  # would be used on miss
    cap_label_mock = AsyncMock(return_value=[
        {"name": "Newly Generated", "description": "new.", "flow_ids": ["f1", "f2"]},
    ])

    new_blob = _blob(
        [_flow(id_="f1", entry="a", file_hash="h"),
         _flow(id_="f2", entry="b", file_hash="h")],
        commit="sha:new",
    )

    flow_labeler._label_flow = flow_label_mock  # type: ignore[attr-defined]
    flow_labeler._label_capabilities = cap_label_mock  # type: ignore[attr-defined]
    try:
        result = await label_flow_blob(
            new_blob, prior_blob=prior, workspace_root=tmp_path,
            nodes_by_id={"a": _node("a"), "b": _node("b")},
            provider=MagicMock(),
        )
    finally:
        from agent.graph_analyzer.flow_labeler import _label_flow as real_lf
        from agent.graph_analyzer.flow_labeler import _label_capabilities as real_lc
        flow_labeler._label_flow = real_lf  # type: ignore[attr-defined]
        flow_labeler._label_capabilities = real_lc  # type: ignore[attr-defined]

    assert len(result.capabilities) == 1
    # Prior wins because hash matched.
    assert result.capabilities[0].name == "Prior Auth"
    assert result.capabilities[0].description == "Old desc."
    assert result.capabilities[0].labeled_at_commit == "sha:old"


@pytest.mark.asyncio
async def test_capability_grouping_failure_falls_back_to_unlabeled(tmp_path: Path):
    """When the LLM grouping call returns [], the result has the Phase 1
    'unlabeled' capability containing every flow."""
    from agent.graph_analyzer import flow_labeler

    flow_labeler._label_flow = AsyncMock(return_value=("F", "f."))  # type: ignore[attr-defined]
    flow_labeler._label_capabilities = AsyncMock(return_value=[])  # type: ignore[attr-defined]

    new_blob = _blob([_flow(id_="f1", entry="x", file_hash="h")])

    try:
        result = await label_flow_blob(
            new_blob, prior_blob=None, workspace_root=tmp_path,
            nodes_by_id={"x": _node("x")}, provider=MagicMock(),
        )
    finally:
        from agent.graph_analyzer.flow_labeler import _label_flow as real_lf
        from agent.graph_analyzer.flow_labeler import _label_capabilities as real_lc
        flow_labeler._label_flow = real_lf  # type: ignore[attr-defined]
        flow_labeler._label_capabilities = real_lc  # type: ignore[attr-defined]

    assert len(result.capabilities) == 1
    assert result.capabilities[0].id == "unlabeled"
    assert result.capabilities[0].flow_ids == ["f1"]


@pytest.mark.asyncio
async def test_labeler_model_persisted_in_blob(tmp_path: Path):
    from agent.graph_analyzer import flow_labeler

    flow_labeler._label_flow = AsyncMock(return_value=("F", "f."))  # type: ignore[attr-defined]
    flow_labeler._label_capabilities = AsyncMock(return_value=[
        {"name": "X", "description": "x.", "flow_ids": ["f1"]},
    ])

    new_blob = _blob([_flow(id_="f1", entry="x", file_hash="h")])

    try:
        result = await label_flow_blob(
            new_blob, prior_blob=None, workspace_root=tmp_path,
            nodes_by_id={"x": _node("x")}, provider=MagicMock(),
            labeler_model="claude-test-model",
        )
    finally:
        from agent.graph_analyzer.flow_labeler import _label_flow as real_lf
        from agent.graph_analyzer.flow_labeler import _label_capabilities as real_lc
        flow_labeler._label_flow = real_lf  # type: ignore[attr-defined]
        flow_labeler._label_capabilities = real_lc  # type: ignore[attr-defined]

    assert result.labeler_model == "claude-test-model"
```

- [ ] **Step 2: Run failing test**

Run: `/Users/alanyeginchibayev/Documents/Github/auto-agent/.venv/bin/python3 -m pytest tests/test_graph_flow_labeler_compose.py -v`

Expected: ImportError on `label_flow_blob`.

- [ ] **Step 3: Implement `label_flow_blob`**

Append to `agent/graph_analyzer/flow_labeler.py`:

```python
import hashlib

from shared.types import Capability, FlowJsonBlob


def _capability_hash(flow_ids: list[str]) -> str:
    joined = ",".join(sorted(flow_ids))
    return f"sha256:{hashlib.sha256(joined.encode('utf-8')).hexdigest()}"


def _phase1_fallback_capability(flow_ids: list[str]) -> Capability:
    return Capability(
        id="unlabeled",
        flow_ids=flow_ids,
        flow_membership_hash=_capability_hash(flow_ids),
        name=None,
        description=None,
        labeled_at_commit=None,
    )


async def label_flow_blob(
    blob: FlowJsonBlob,
    prior_blob: FlowJsonBlob | None,
    workspace_root: Path,
    nodes_by_id: dict[str, Node],
    provider: "LLMProvider",
    *,
    labeler_model: str = "claude-haiku-4-5",
) -> FlowJsonBlob:
    """Phase 2 entry point: label flows + capabilities in *blob*.

    Reuses prior labels whose ``file_set_hash`` (per-flow) or
    ``flow_membership_hash`` (per-capability) matches the supplied
    *prior_blob*. Falls back to the Phase 1 single-"unlabeled" capability
    shape if the LLM grouping call fails.

    Returns a *new* :class:`FlowJsonBlob`. The input blob is not mutated.
    """
    # Build a lookup of prior flows by id for cache checks.
    prior_flows_by_id: dict[str, Flow] = {}
    if prior_blob is not None:
        prior_flows_by_id = {f.id: f for f in prior_blob.flows}

    labelled_flows: list[Flow] = []
    for flow in blob.flows:
        prior = prior_flows_by_id.get(flow.id)
        if (
            prior is not None
            and prior.file_set_hash == flow.file_set_hash
            and prior.name is not None
            and prior.description is not None
        ):
            labelled_flows.append(
                flow.model_copy(
                    update={
                        "name": prior.name,
                        "description": prior.description,
                        "labeled_at_commit": prior.labeled_at_commit,
                    },
                ),
            )
            continue

        slices = _load_file_slices(workspace_root, flow.steps, nodes_by_id)
        name, description = await _label_flow(provider, flow, slices)
        labelled_flows.append(
            flow.model_copy(
                update={
                    "name": name,
                    "description": description,
                    "labeled_at_commit": blob.derived_at_commit if name else None,
                },
            ),
        )

    # Capability grouping over the now-labelled flows.
    cap_dicts = await _label_capabilities(provider, labelled_flows)

    if not cap_dicts:
        capabilities = [
            _phase1_fallback_capability([f.id for f in labelled_flows]),
        ]
    else:
        # Build prior capabilities by membership hash for cache.
        prior_caps_by_hash: dict[str, Capability] = {}
        if prior_blob is not None:
            prior_caps_by_hash = {
                c.flow_membership_hash: c for c in prior_blob.capabilities
            }

        capabilities = []
        for i, cap in enumerate(cap_dicts):
            flow_ids: list[str] = cap["flow_ids"]  # type: ignore[assignment]
            mh = _capability_hash(flow_ids)
            prior_cap = prior_caps_by_hash.get(mh)
            if prior_cap is not None and prior_cap.name is not None:
                capabilities.append(
                    Capability(
                        id=prior_cap.id,
                        flow_ids=flow_ids,
                        flow_membership_hash=mh,
                        name=prior_cap.name,
                        description=prior_cap.description,
                        labeled_at_commit=prior_cap.labeled_at_commit,
                    ),
                )
            else:
                capabilities.append(
                    Capability(
                        id=f"cap_{i}_{mh[7:15]}",  # stable, derived from hash prefix
                        flow_ids=flow_ids,
                        flow_membership_hash=mh,
                        name=cap["name"],  # type: ignore[arg-type]
                        description=cap["description"],  # type: ignore[arg-type]
                        labeled_at_commit=blob.derived_at_commit,
                    ),
                )

    return FlowJsonBlob(
        capabilities=capabilities,
        flows=labelled_flows,
        unreached=blob.unreached,
        derived_at_commit=blob.derived_at_commit,
        deriver_version=blob.deriver_version,
        labeler_model=labeler_model,
    )


__all__ = [
    "MAX_LINES_PER_STEP",
    "_label_capabilities",
    "_label_flow",
    "_load_file_slices",
    "label_flow_blob",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/Users/alanyeginchibayev/Documents/Github/auto-agent/.venv/bin/python3 -m pytest tests/test_graph_flow_labeler_compose.py -v`

Expected: all 5 tests pass.

- [ ] **Step 5: Lint**

Run: `ruff check agent/graph_analyzer/flow_labeler.py tests/test_graph_flow_labeler_compose.py`

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add agent/graph_analyzer/flow_labeler.py tests/test_graph_flow_labeler_compose.py
git commit -m "feat(graph): label_flow_blob — compose flow+capability labelling with caches"
```

---

## Task 6 — Wire labelling into the recompute endpoint

**Files:**
- Modify: `orchestrator/router.py` (the recompute endpoint)
- Test: `tests/test_repo_graph_flows_endpoint_phase2.py`

Endpoint changes:
1. After `derive_flow_blob(...)`, read the prior `flow_json` from the same `RepoGraph` row (it may be None — first recompute).
2. Resolve a provider via `get_structured_extractor_provider()`.
3. Build `nodes_by_id` from `RepoGraphBlob.nodes`.
4. Call `label_flow_blob(new_blob, prior_blob, workspace_root, nodes_by_id, provider)`.
5. Persist the labelled blob.

Add to the `RecomputeFlowsResponse`:
- `labeled_flow_count: int` (how many flows got names) — useful for the user to see the LLM did work.

- [ ] **Step 1: Add failing test**

Create `tests/test_repo_graph_flows_endpoint_phase2.py`:

```python
"""Phase 2 integration: recompute endpoint calls labeller and persists
labelled output. The labeller is mocked so this test stays hermetic."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agent.graph_analyzer.flows import derive_flow_blob
from orchestrator.router import recompute_repo_graph_flows
from shared.types import (
    Capability,
    Edge,
    EdgeEvidence,
    EntryPoint,
    Flow,
    FlowJsonBlob,
    FlowStep,
    Node,
    RepoGraphBlob,
)


# Reuse test scaffolding helpers from test_repo_graph_flows_endpoint.py
# (copy or import — match whichever conftest convention is used).


@pytest.mark.asyncio
async def test_endpoint_calls_labeller_and_persists_labelled_blob(tmp_path: Path):
    """The recompute endpoint should derive the blob, pass it (along
    with prior blob and workspace) into label_flow_blob, and persist
    the labelled result."""

    # ---- arrange a session that returns a real RepoGraph row ----
    graph_blob = RepoGraphBlob(
        commit_sha="sha:new",
        generated_at=datetime.now(tz=UTC),
        analyser_version="test",
        areas=[],
        nodes=[
            Node(id="api/x.py::handler", kind="function", label="handler",
                 file="api/x.py", line_start=1, line_end=3, area="api"),
            Node(id="web/x.tsx::call", kind="function", label="call",
                 file="web/x.tsx", area="web"),
        ],
        edges=[
            Edge(source="web/x.tsx::call", target="api/x.py::handler",
                 kind="http", source_kind="ast",
                 evidence=EdgeEvidence(file="web/x.tsx", line=1, snippet="fetch")),
        ],
    )

    # Existing row carries no flow_json yet (first label).
    row = MagicMock()
    row.graph_json = graph_blob.model_dump(mode="json")
    row.flow_json = None
    row.id = 42

    session = AsyncMock(spec=AsyncSession)
    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=MagicMock(id=1))  # repo
    second_result = MagicMock()
    second_result.scalar_one_or_none = MagicMock(return_value=row)
    session.execute = AsyncMock(side_effect=[execute_result, second_result])

    # Mock label_flow_blob to return a known-shape result.
    expected_labelled = FlowJsonBlob(
        capabilities=[
            Capability(
                id="cap_0", flow_ids=["f1"],
                flow_membership_hash="sha256:test",
                name="Auth", description="Sign-in.",
                labeled_at_commit="sha:new",
            ),
        ],
        flows=[
            Flow(
                id="f1",
                entry_point=EntryPoint(node_id="api/x.py::handler", kind="http"),
                terminal_node_id="api/x.py::handler",
                terminal_kind="response",
                steps=[FlowStep(node_id="api/x.py::handler", depth=0)],
                file_set=["api/x.py"],
                file_set_hash="sha256:test",
                name="Login Flow",
                description="Auth.",
                labeled_at_commit="sha:new",
            ),
        ],
        unreached=[],
        derived_at_commit="sha:new",
        deriver_version="phase1",
        labeler_model="claude-haiku-4-5",
    )

    with (
        patch("orchestrator.router._get_repo_in_org",
              AsyncMock(return_value=MagicMock(id=1))),
        patch("agent.graph_workspace.graph_workspace_path",
              return_value=str(tmp_path)),
        patch("agent.graph_analyzer.flow_labeler.label_flow_blob",
              AsyncMock(return_value=expected_labelled)) as labeller_mock,
        patch("agent.llm.structured.get_structured_extractor_provider",
              return_value=MagicMock()),
    ):
        result = await recompute_repo_graph_flows(
            repo_id=1, session=session, org_id=1,
        )

    labeller_mock.assert_awaited_once()
    # The labeller received None as prior_blob (first label).
    call_kwargs = labeller_mock.call_args.kwargs
    assert call_kwargs.get("prior_blob") is None
    # The row's flow_json was set to the labelled blob.
    assert row.flow_json is not None
    assert row.flow_json["labeler_model"] == "claude-haiku-4-5"
    # Response surfaces labeled_flow_count.
    assert result.labeled_flow_count == 1
    assert result.flow_count == 1


@pytest.mark.asyncio
async def test_endpoint_passes_prior_blob_on_second_recompute(tmp_path: Path):
    """When the row already has flow_json, it's passed as prior_blob
    so the labeller can apply cache hits."""

    # Arrange existing flow_json on the row.
    prior_labelled = FlowJsonBlob(
        capabilities=[],
        flows=[],
        unreached=[],
        derived_at_commit="sha:old",
        deriver_version="phase1",
        labeler_model="claude-haiku-4-5",
    )

    graph_blob = RepoGraphBlob(
        commit_sha="sha:new",
        generated_at=datetime.now(tz=UTC),
        analyser_version="test",
        areas=[], nodes=[], edges=[],
    )

    row = MagicMock()
    row.graph_json = graph_blob.model_dump(mode="json")
    row.flow_json = prior_labelled.model_dump(mode="json")

    session = AsyncMock(spec=AsyncSession)
    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=MagicMock(id=1))
    second_result = MagicMock()
    second_result.scalar_one_or_none = MagicMock(return_value=row)
    session.execute = AsyncMock(side_effect=[execute_result, second_result])

    labelled_back = prior_labelled.model_copy(update={"derived_at_commit": "sha:new"})

    with (
        patch("orchestrator.router._get_repo_in_org",
              AsyncMock(return_value=MagicMock(id=1))),
        patch("agent.graph_workspace.graph_workspace_path",
              return_value=str(tmp_path)),
        patch("agent.graph_analyzer.flow_labeler.label_flow_blob",
              AsyncMock(return_value=labelled_back)) as labeller_mock,
        patch("agent.llm.structured.get_structured_extractor_provider",
              return_value=MagicMock()),
    ):
        await recompute_repo_graph_flows(repo_id=1, session=session, org_id=1)

    # Verify prior_blob was passed.
    call_kwargs = labeller_mock.call_args.kwargs
    assert call_kwargs.get("prior_blob") is not None
    assert call_kwargs["prior_blob"].derived_at_commit == "sha:old"
```

- [ ] **Step 2: Update `RecomputeFlowsResponse`**

Edit `shared/types.py`. Add to the existing `RecomputeFlowsResponse`:

```python
class RecomputeFlowsResponse(BaseModel):
    """``POST /api/repos/{id}/graph/flows/recompute`` response body."""

    repo_id: int
    flow_count: int
    capability_count: int
    unreached_count: int
    derived_at_commit: str
    labeled_flow_count: int = 0
    """Number of flows that received a non-null name from the Phase 2
    labeller. 0 in Phase 1; matches ``flow_count`` once all flows label
    successfully."""
```

- [ ] **Step 3: Wire labelling into the endpoint**

Edit the recompute endpoint in `orchestrator/router.py`. After `derive_flow_blob(...)`, before the persist:

```python
    # Phase 2: LLM labelling.
    from agent.graph_analyzer.flow_labeler import label_flow_blob
    from agent.llm.structured import get_structured_extractor_provider

    prior_blob: FlowJsonBlob | None = None
    if row.flow_json is not None:
        try:
            prior_blob = FlowJsonBlob.model_validate(row.flow_json)
        except Exception:  # noqa: BLE001 — defensive against stale Phase 1 shapes
            prior_blob = None

    nodes_by_id = {n.id: n for n in blob.nodes}
    provider = get_structured_extractor_provider()
    labelled = await label_flow_blob(
        flow_blob,
        prior_blob=prior_blob,
        workspace_root=workspace_path,
        nodes_by_id=nodes_by_id,
        provider=provider,
    )

    row.flow_json = labelled.model_dump(mode="json")
    await session.commit()

    return RecomputeFlowsResponse(
        repo_id=repo_id,
        flow_count=len(labelled.flows),
        capability_count=len(labelled.capabilities),
        unreached_count=len(labelled.unreached),
        derived_at_commit=labelled.derived_at_commit,
        labeled_flow_count=sum(1 for f in labelled.flows if f.name is not None),
    )
```

Add `FlowJsonBlob` to the imports if not already present.

- [ ] **Step 4: Run the test**

Run: `/Users/alanyeginchibayev/Documents/Github/auto-agent/.venv/bin/python3 -m pytest tests/test_repo_graph_flows_endpoint_phase2.py tests/test_repo_graph_flows_endpoint.py -v`

Expected: new + existing tests pass.

- [ ] **Step 5: Lint**

Run: `ruff check orchestrator/router.py shared/types.py tests/test_repo_graph_flows_endpoint_phase2.py`

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/router.py shared/types.py tests/test_repo_graph_flows_endpoint_phase2.py
git commit -m "feat(graph): wire LLM labelling into recompute endpoint (Phase 2)"
```

---

## Task 7 — Final sweep

**Files:**
- None (verification only)

- [ ] **Step 1: Full pytest**

Run: `/Users/alanyeginchibayev/Documents/Github/auto-agent/.venv/bin/python3 -m pytest tests/ -q --tb=no 2>&1 | tail -10`

Expected: all tests pass except the 3 known pre-existing failures from Phase 1 (architect prompt drift, slack DNS, botocore missing). No new failures.

- [ ] **Step 2: Ruff check**

Run: `ruff check .`

Expected: no new errors beyond the pre-existing UP042 entries on enum classes.

- [ ] **Step 3: Ruff format check**

Run: `ruff format --check .`

If files need formatting: `ruff format .` then `git add -A && git commit -m "style: ruff format Phase 2"`.

- [ ] **Step 4: Final inspection**

```bash
git diff main --stat
git log main..HEAD --oneline
```

Confirm files match the "File structure" block at the top of this plan. New: `agent/graph_analyzer/flow_labeler.py`, 4 new test files. Modified: `shared/types.py`, `orchestrator/router.py`.

---

## What lands after Phase 2

- A repo with `flow_json` already populated gets human-readable capability + flow names on the next recompute.
- Per-flow LLM calls are skipped for flows whose `file_set_hash` is unchanged → cheap re-runs.
- Capability grouping runs once per recompute; emitted capabilities whose `flow_membership_hash` matches a prior capability inherit the prior name (stable mental map).
- The `which_capability` agent op now returns real names — agent reasoning gets product-language ("touches Authentication") for free, no code change needed in the tool.

Phase 3 (UI `map-canvas.tsx`, LOD 0 + LOD 1 + tab bar) consumes this. Phase 4 (LOD 2 step chains + LOD 3 source + boundary ports) follows.
