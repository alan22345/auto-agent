# Auto-Heal Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an autonomous loop that continuously drains code-graph health findings — batching, fixing, verifying (CI + smoke + differential), and staging each batch onto a rebased cleanup branch — while holding a VM-global exclusive lease so it never saturates the box.

**Architecture:** A supervisor task ranks findings worst-first, bundles up to `batch_size` into one fix task, runs it through three gates, and merges the result onto a long-lived cleanup branch kept rebased on `main`. Builds on the now-fail-closed verify gates (commit `52964d6`). Spec: `docs/superpowers/specs/2026-06-09-auto-heal-loop-design.md`.

**Tech Stack:** Python 3.12, async SQLAlchemy, Pydantic, Redis (lease), FastAPI (`orchestrator/router.py`), Next.js/TS (`web-next`), pytest.

---

## Phase roadmap

This feature spans several independently-testable subsystems. It is broken into
**six sequenced phases**, each producing working, tested software on its own.
**This document fully details Phase 1**; subsequent phases get their own plan
docs (`...-auto-heal-loop-phase2.md`, etc.) authored as each prior phase lands,
so each plan is grounded in the code the previous phase actually produced.

| Phase | Subsystem | Produces |
|-------|-----------|----------|
| **1** | **Finding ranking + identity + dedup/suppression** (this doc) | `agent/health_loop/findings.py` — pure, no I/O |
| 2 | Differential verifier | before/after route+screenshot diff → regress verdict |
| 3 | CleanupBranchManager | rebase/merge mechanics + scoped force-push carve-out |
| 4 | Supervisor task + VM-global lease + Stop/Resume + dispatcher block | the loop runtime |
| 5 | Batch translator + dedicated health-fix handler wiring the 3 gates | end-to-end fix cycle |
| 6 | API endpoints + web-next health-tab toggle/status/suppress | user-facing control |

Phase 1 is pure (a `RepoGraphBlob` in, an ordered finding list out) so it is the
safest place to start and everything downstream consumes its `HealthFinding`
type and `finding_hash`.

---

## Phase 1 — Finding ranking + identity + dedup/suppression

### File structure

- **Create:** `agent/health_loop/__init__.py` — new package marker (empty).
- **Create:** `agent/health_loop/findings.py` — the `HealthFinding` model,
  `finding_hash`, `extract_findings`, `rank_findings`, `select_batch`. One file,
  one responsibility (turn a blob into a ranked, filtered finding list). No I/O,
  no DB — pure functions over `shared.types` models, so it is trivially testable.
- **Create:** `tests/test_health_loop_findings.py` — unit tests.

### Reference: the input types (already in `shared/types.py`)

These are the blob fields Phase 1 reads. Do **not** redefine them — import from
`shared.types`:

```python
# RepoGraphBlob fields used here:
#   dead_code:   list[DeadCodeFinding]   # kind, target, file: str|None, reason
#   cycles:      list[DependencyCycle]   # id, kind, members: list[str], closing_edges
#   clones:      list[CloneGroup]        # id, token_len: int, mode, instances, family_id: str|None
#                                        #   CloneInstance: file, line_start, line_end
#   hotspots:    list[Hotspot]           # file, churn, complexity_density, score: float, trend
#   file_health: list[FileHealth]        # file, maintainability_index: float, band, crap
#   health:      RepoHealth | None
```

---

### Task 1: `HealthFinding` model + category weights

**Files:**
- Create: `agent/health_loop/__init__.py`
- Create: `agent/health_loop/findings.py`
- Test: `tests/test_health_loop_findings.py`

- [ ] **Step 1: Create the empty package marker**

Create `agent/health_loop/__init__.py` with a one-line docstring:

```python
"""Auto-heal loop — ranks code-graph health findings and drives fixes."""
```

- [ ] **Step 2: Write the failing test for the model + weights**

Create `tests/test_health_loop_findings.py`:

```python
"""Phase 1 — pure finding ranking / identity / filtering."""
from __future__ import annotations

from agent.health_loop.findings import (
    CATEGORY_WEIGHTS,
    HealthFinding,
)


def test_health_finding_is_frozen_and_carries_core_fields():
    f = HealthFinding(
        finding_hash="abc123",
        category="dead_code",
        title="unused export api/routes.py::helper",
        files=["api/routes.py"],
        severity=1.0,
    )
    assert f.finding_hash == "abc123"
    assert f.category == "dead_code"
    assert f.files == ["api/routes.py"]


def test_category_weights_match_composite_health_weighting():
    # poor_file→maintainability .30, dead_code .25, clone→duplication .20,
    # hotspot .15 (coupling's slot — no finding list of its own), cycle .10
    assert CATEGORY_WEIGHTS == {
        "poor_file": 0.30,
        "dead_code": 0.25,
        "clone": 0.20,
        "hotspot": 0.15,
        "cycle": 0.10,
    }
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_findings.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.health_loop.findings'`

- [ ] **Step 4: Implement the model + weights**

Create `agent/health_loop/findings.py`:

```python
"""Rank code-graph health findings into an ordered, deduplicated work list.

Pure functions over :mod:`shared.types` — no DB, no I/O. Given a
``RepoGraphBlob`` they produce a list of :class:`HealthFinding` ordered
worst-first, each carrying a stable ``finding_hash`` so the loop never
double-files or re-picks a suppressed finding.
"""
from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel

Category = Literal["poor_file", "dead_code", "clone", "hotspot", "cycle"]

# Mirrors the composite-health sub-score weighting in
# agent/graph_analyzer/health.py. 'coupling' (0.15) has no per-item finding
# list of its own, so 'hotspot' takes that slot.
CATEGORY_WEIGHTS: dict[Category, float] = {
    "poor_file": 0.30,
    "dead_code": 0.25,
    "clone": 0.20,
    "hotspot": 0.15,
    "cycle": 0.10,
}


class HealthFinding(BaseModel, frozen=True):
    """One actionable health finding, normalized across categories.

    ``finding_hash`` is stable across re-analyses (see :func:`finding_hash`)
    so it doubles as the dedup / suppression key. ``severity`` is the
    in-category magnitude (higher = worse), used as the secondary sort key.
    """

    finding_hash: str
    category: Category
    title: str
    files: list[str]
    severity: float
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_findings.py -q`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add agent/health_loop/__init__.py agent/health_loop/findings.py tests/test_health_loop_findings.py
git commit -m "feat(health-loop): HealthFinding model + category weights"
```

---

### Task 2: `finding_hash` — stable identity per category

**Files:**
- Modify: `agent/health_loop/findings.py`
- Test: `tests/test_health_loop_findings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health_loop_findings.py`:

```python
from agent.health_loop.findings import finding_hash


def test_finding_hash_is_stable_and_order_independent():
    # Same logical finding → same hash, regardless of member ordering.
    h1 = finding_hash("cycle", ["a.py::x", "b.py::y"])
    h2 = finding_hash("cycle", ["b.py::y", "a.py::x"])
    assert h1 == h2
    assert len(h1) == 16  # truncated hex digest


def test_finding_hash_distinguishes_category_and_payload():
    assert finding_hash("dead_code", ["api/routes.py::helper"]) != finding_hash(
        "dead_code", ["api/routes.py::other"]
    )
    # Same payload, different category → different hash.
    assert finding_hash("dead_code", ["x"]) != finding_hash("hotspot", ["x"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_findings.py -k finding_hash -q`
Expected: FAIL — `ImportError: cannot import name 'finding_hash'`

- [ ] **Step 3: Implement `finding_hash`**

Add to `agent/health_loop/findings.py` (after `CATEGORY_WEIGHTS`):

```python
def finding_hash(category: Category, parts: list[str]) -> str:
    """Stable 16-char identity for a finding.

    ``parts`` are the identity-bearing strings for the category (e.g. the
    dead-code target, the sorted cycle members, the clone family). Sorted
    before hashing so member ordering can't change the hash.
    """
    canonical = category + "|" + "|".join(sorted(parts))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_findings.py -k finding_hash -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/health_loop/findings.py tests/test_health_loop_findings.py
git commit -m "feat(health-loop): stable finding_hash identity"
```

---

### Task 3: `extract_findings` — blob → normalized findings

**Files:**
- Modify: `agent/health_loop/findings.py`
- Test: `tests/test_health_loop_findings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health_loop_findings.py`:

```python
from shared.types import (
    CloneGroup,
    CloneInstance,
    DeadCodeFinding,
    DependencyCycle,
    FileHealth,
    Hotspot,
    RepoGraphBlob,
)
from agent.health_loop.findings import extract_findings


def _blob(**kw) -> RepoGraphBlob:
    base = dict(
        nodes=[], edges=[], dead_code=[], cycles=[], clones=[],
        hotspots=[], file_health=[], health=None,
    )
    base.update(kw)
    return RepoGraphBlob(**base)


def test_extract_covers_every_category():
    blob = _blob(
        dead_code=[DeadCodeFinding(kind="unused_export", target="a.py::h", file="a.py", reason="never imported")],
        cycles=[DependencyCycle(id="c1", kind="import", members=["a.py", "b.py"], closing_edges=[])],
        clones=[CloneGroup(id="g1", token_len=120, mode="strict", instances=[
            CloneInstance(file="a.py", line_start=1, line_end=9),
            CloneInstance(file="b.py", line_start=1, line_end=9),
        ], family_id=None)],
        hotspots=[Hotspot(file="a.py", churn=5.0, complexity_density=0.4, score=80.0, trend="accelerating")],
        file_health=[
            FileHealth(file="a.py", maintainability_index=20.0, band="poor"),
            FileHealth(file="ok.py", maintainability_index=90.0, band="good"),
        ],
    )
    found = extract_findings(blob)
    cats = {f.category for f in found}
    assert cats == {"dead_code", "cycle", "clone", "hotspot", "poor_file"}
    # 'good'-band files are not findings.
    assert all("ok.py" not in f.files for f in found if f.category == "poor_file")


def test_extract_severity_reflects_magnitude():
    blob = _blob(file_health=[
        FileHealth(file="worse.py", maintainability_index=10.0, band="poor"),
        FileHealth(file="bad.py", maintainability_index=35.0, band="poor"),
    ])
    found = {f.files[0]: f for f in extract_findings(blob)}
    # Lower maintainability index ⇒ higher severity.
    assert found["worse.py"].severity > found["bad.py"].severity
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_findings.py -k extract -q`
Expected: FAIL — `ImportError: cannot import name 'extract_findings'`

- [ ] **Step 3: Implement `extract_findings`**

Add to `agent/health_loop/findings.py`:

```python
from shared.types import RepoGraphBlob


def extract_findings(blob: RepoGraphBlob) -> list[HealthFinding]:
    """Flatten a blob into normalized :class:`HealthFinding` records.

    One finding per dead-code item, cycle, clone group, hotspot, and
    'poor'-band file. 'moderate'/'good' files are not findings.
    """
    out: list[HealthFinding] = []

    for d in blob.dead_code:
        out.append(HealthFinding(
            finding_hash=finding_hash("dead_code", [d.target]),
            category="dead_code",
            title=f"{d.kind}: {d.target} — {d.reason}",
            files=[d.file] if d.file else [],
            severity=1.0,
        ))

    for c in blob.cycles:
        out.append(HealthFinding(
            finding_hash=finding_hash("cycle", list(c.members)),
            category="cycle",
            title=f"import cycle [{c.kind}]: {' → '.join(c.members)}",
            files=list(dict.fromkeys(m.split('::')[0] for m in c.members)),
            severity=float(len(c.members)),
        ))

    for g in blob.clones:
        files = list(dict.fromkeys(inst.file for inst in g.instances))
        out.append(HealthFinding(
            finding_hash=finding_hash("clone", [g.family_id] if g.family_id else
                                      [f"{inst.file}:{inst.line_start}-{inst.line_end}" for inst in g.instances]),
            category="clone",
            title=f"clone group {g.id} — {g.token_len} tokens, {len(g.instances)} instances",
            files=files,
            severity=float(g.token_len),
        ))

    for h in blob.hotspots:
        out.append(HealthFinding(
            finding_hash=finding_hash("hotspot", [h.file]),
            category="hotspot",
            title=f"hotspot {h.file} — score {h.score:.1f} ({h.trend})",
            files=[h.file],
            severity=float(h.score),
        ))

    for fh in blob.file_health:
        if fh.band != "poor":
            continue
        out.append(HealthFinding(
            finding_hash=finding_hash("poor_file", [fh.file]),
            category="poor_file",
            title=f"poor maintainability {fh.file} — index {fh.maintainability_index:.1f}",
            files=[fh.file],
            severity=100.0 - fh.maintainability_index,
        ))

    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_findings.py -k extract -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/health_loop/findings.py tests/test_health_loop_findings.py
git commit -m "feat(health-loop): extract_findings normalizes blob into findings"
```

---

### Task 4: `rank_findings` — worst-first, deterministic

**Files:**
- Modify: `agent/health_loop/findings.py`
- Test: `tests/test_health_loop_findings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health_loop_findings.py`:

```python
from agent.health_loop.findings import rank_findings


def test_rank_orders_by_category_weight_then_severity():
    blob = _blob(
        cycles=[DependencyCycle(id="c1", kind="import", members=["a.py", "b.py"], closing_edges=[])],  # weight .10
        file_health=[FileHealth(file="a.py", maintainability_index=10.0, band="poor")],  # weight .30
    )
    ranked = rank_findings(blob)
    # poor_file (.30) outranks cycle (.10) regardless of severity magnitude.
    assert ranked[0].category == "poor_file"
    assert ranked[-1].category == "cycle"


def test_rank_is_deterministic():
    blob = _blob(dead_code=[
        DeadCodeFinding(kind="unused_export", target=f"a.py::h{i}", file="a.py", reason="x")
        for i in range(5)
    ])
    assert [f.finding_hash for f in rank_findings(blob)] == [
        f.finding_hash for f in rank_findings(blob)
    ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_findings.py -k rank -q`
Expected: FAIL — `ImportError: cannot import name 'rank_findings'`

- [ ] **Step 3: Implement `rank_findings`**

Add to `agent/health_loop/findings.py`:

```python
def rank_findings(blob: RepoGraphBlob) -> list[HealthFinding]:
    """Return findings worst-first.

    Primary key: category weight (higher first). Secondary: in-category
    severity (higher first). Tertiary: ``finding_hash`` for a stable,
    deterministic total order.
    """
    return sorted(
        extract_findings(blob),
        key=lambda f: (-CATEGORY_WEIGHTS[f.category], -f.severity, f.finding_hash),
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_findings.py -k rank -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/health_loop/findings.py tests/test_health_loop_findings.py
git commit -m "feat(health-loop): rank_findings worst-first, deterministic"
```

---

### Task 5: `select_batch` — filter suppressed/in-flight, take N

**Files:**
- Modify: `agent/health_loop/findings.py`
- Test: `tests/test_health_loop_findings.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health_loop_findings.py`:

```python
from agent.health_loop.findings import select_batch


def test_select_batch_excludes_suppressed_and_in_flight_and_caps_at_n():
    blob = _blob(dead_code=[
        DeadCodeFinding(kind="unused_export", target=f"a.py::h{i}", file="a.py", reason="x")
        for i in range(10)
    ])
    ranked = rank_findings(blob)
    suppressed = {ranked[0].finding_hash}
    in_flight = {ranked[1].finding_hash}

    batch = select_batch(blob, suppressed=suppressed, in_flight=in_flight, batch_size=3)

    hashes = {f.finding_hash for f in batch}
    assert len(batch) == 3
    assert ranked[0].finding_hash not in hashes  # suppressed excluded
    assert ranked[1].finding_hash not in hashes  # in-flight excluded


def test_select_batch_empty_when_all_filtered():
    blob = _blob(dead_code=[
        DeadCodeFinding(kind="unused_export", target="a.py::h", file="a.py", reason="x")
    ])
    only = rank_findings(blob)[0].finding_hash
    assert select_batch(blob, suppressed={only}, in_flight=set(), batch_size=5) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_findings.py -k select_batch -q`
Expected: FAIL — `ImportError: cannot import name 'select_batch'`

- [ ] **Step 3: Implement `select_batch`**

Add to `agent/health_loop/findings.py`:

```python
def select_batch(
    blob: RepoGraphBlob,
    *,
    suppressed: set[str],
    in_flight: set[str],
    batch_size: int,
) -> list[HealthFinding]:
    """Top ``batch_size`` ranked findings, excluding suppressed/in-flight.

    The exclusion is per-finding (by ``finding_hash``), so a batch never
    re-files a finding that is already being worked or has been suppressed.
    """
    skip = suppressed | in_flight
    eligible = [f for f in rank_findings(blob) if f.finding_hash not in skip]
    return eligible[: max(0, batch_size)]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_findings.py -k select_batch -q`
Expected: PASS

- [ ] **Step 5: Run the full Phase 1 test file + lint**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_findings.py -q`
Expected: PASS (all)
Run: `.venv/bin/ruff check agent/health_loop/ tests/test_health_loop_findings.py`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add agent/health_loop/findings.py tests/test_health_loop_findings.py
git commit -m "feat(health-loop): select_batch with suppression + in-flight filtering"
```

---

### Phase 1 exit criteria

- `agent/health_loop/findings.py` exposes `HealthFinding`, `CATEGORY_WEIGHTS`,
  `finding_hash`, `extract_findings`, `rank_findings`, `select_batch`.
- All pure (no DB/I/O), fully unit-tested, ruff-clean.
- Downstream phases consume `HealthFinding`/`finding_hash`/`select_batch`
  unchanged.

### Verify the input types before coding Phase 1

Before Task 3, open `shared/types.py` and confirm the exact field names on
`DeadCodeFinding`, `DependencyCycle`, `CloneGroup`/`CloneInstance`, `Hotspot`,
`FileHealth`, and the constructor signature of `RepoGraphBlob` (required fields
for the `_blob` test helper). If any differ from the Reference block above,
adjust the test helper and `extract_findings` to match — the rest of the plan is
unaffected.

---

## Next phases (authored after Phase 1 lands)

- **Phase 2 (Differential verifier):** `agent/health_loop/differential.py` —
  given base + branch workspaces, boot each, capture `exercise_routes` +
  `inspect_ui` for known routes, diff, return a regress/no-regress verdict.
  Reuses `agent/lifecycle/verify_primitives.py`. TDD with mocked boot/exercise.
- **Phase 3 (CleanupBranchManager):** rebase-onto-main + merge-accepted-fix +
  the scoped git force-push carve-out (allowlisted branch name in
  `agent/tools/git.py`). TDD against a temp git repo.
- **Phase 4 (Supervisor + lease):** the `health_loop` supervisor task, the
  VM-global Redis `vm_exclusive_lease` (TTL-guarded), dispatcher block so no
  other task runs while held, `state` machine, Stop/Resume.
- **Phase 5 (Batch handler):** translate a `select_batch` result into one
  health-fix task (`source_id="health:{repo}:batch:{batch_hash}"`,
  `health_finding_hashes` column), run coder + the 3 gates, merge-or-park.
- **Phase 6 (API + UI):** `/repos/{id}/health-loop/{start,stop,resume,suppress}`
  + the health-tab toggle, status strip, and per-row suppress action.
```

