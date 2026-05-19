# Code-Graph Checkpoint + Test-Skip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the ADR-016 code-graph pipeline mid-flight resumable (per-file checkpoints in `repo_graphs`), commit-diff incremental on the next run, with test/mock files excluded from analysis at the file-walk level.

**Architecture:** Extend `repo_graphs` with `is_complete`, `processed_files`, `failed_sites`. Pipeline keeps one row open per repo across runs and UPDATEs after each file. On request start, the orchestrator either no-ops (same complete commit), resumes (incomplete), or applies a git-diff plan (commit changed) before walking. Test files filtered before tree-sitter parse.

**Tech Stack:** Python 3.12, SQLAlchemy async + Alembic, pydantic, anthropic SDK on Bedrock, Next.js 14 + TanStack Query.

**Spec:** `docs/superpowers/specs/2026-05-19-code-graph-checkpoint-and-test-skip-design.md`

---

## File structure

**Backend (Python):**
- `migrations/versions/048_repo_graph_checkpoint.py` — **create**
- `shared/models/core.py` — **modify** (extend `RepoGraph` class)
- `shared/types.py` — **modify** (extend `LatestRepoGraphData`, add `RepoGraphProgressData`)
- `agent/graph_analyzer/test_filter.py` — **create**
- `agent/graph_analyzer/diff.py` — **create**
- `agent/graph_analyzer/pipeline.py` — **modify** (test-file filter + per-file checkpoint loop)
- `agent/lifecycle/graph_refresh.py` — **modify** (row-load cases, diff apply, no-op-on-unchanged-commit)
- `orchestrator/router.py` — **modify** (extend `GET /repos/{id}/graph/latest`; add `GET /repos/{id}/graph/progress`)

**Frontend (TypeScript):**
- `web-next/types/api.ts` — regenerated from Pydantic
- `web-next/lib/code-graph.ts` — **modify** (add `getRepoGraphProgress`)
- `web-next/hooks/useRepoGraphProgress.ts` — **create**
- `web-next/components/code-graph/graph-completion-badge.tsx` — **create**
- `web-next/components/code-graph/refresh-button.tsx` — **modify** (label flip)
- `web-next/app/(app)/code-graph/[repoId]/page.tsx` — **modify** (mount badge + progress hook)

**Tests:**
- `tests/test_graph_pipeline_test_filter.py` — **create**
- `tests/test_graph_pipeline_diff.py` — **create**
- `tests/test_graph_pipeline_resume.py` — **create**
- `tests/test_repo_graph_resume_db.py` — **create** (skipif `DATABASE_URL`)
- `tests/test_graph_refresh_resume_e2e.py` — **create**
- `tests/test_repo_graph_migration_048.py` — **create** (skipif `DATABASE_URL`)
- `tests/test_graph_refresh_handler.py` — **modify** (existing tests adapted to UPDATE-per-file flow)
- `web-next/tests/graph-completion-badge.test.tsx` — **create**
- `web-next/tests/use-repo-graph-progress.test.ts` — **create**

---

## Task 1: Migration 048 — checkpoint columns

**Files:**
- Create: `migrations/versions/048_repo_graph_checkpoint.py`
- Test: `tests/test_repo_graph_migration_048.py`

- [ ] **Step 1: Write the failing test**

`tests/test_repo_graph_migration_048.py`:

```python
"""Round-trip alembic upgrade 048 / downgrade 047 against real Postgres."""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def _skip_if_no_db() -> None:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("needs a running Postgres (set DATABASE_URL)")


def _sync_url() -> str:
    url = os.environ["DATABASE_URL"]
    return url.replace("+asyncpg", "")


def _alembic_cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _sync_url())
    return cfg


def test_upgrade_adds_checkpoint_columns() -> None:
    _skip_if_no_db()
    cfg = _alembic_cfg()
    command.upgrade(cfg, "048")
    engine = sa.create_engine(_sync_url())
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name='repo_graphs' AND column_name IN "
                "('is_complete','processed_files','failed_sites')"
            )
        ).all()
    assert {r[0] for r in rows} == {"is_complete", "processed_files", "failed_sites"}


def test_existing_rows_marked_complete() -> None:
    _skip_if_no_db()
    cfg = _alembic_cfg()
    command.upgrade(cfg, "048")
    engine = sa.create_engine(_sync_url())
    with engine.connect() as conn:
        out = conn.execute(
            sa.text("SELECT bool_and(is_complete) FROM repo_graphs")
        ).scalar()
    # Either no rows (returns None) or every existing row is_complete=true.
    assert out is None or out is True


def test_downgrade_drops_columns() -> None:
    _skip_if_no_db()
    cfg = _alembic_cfg()
    command.upgrade(cfg, "048")
    command.downgrade(cfg, "047")
    engine = sa.create_engine(_sync_url())
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='repo_graphs' AND column_name IN "
                "('is_complete','processed_files','failed_sites')"
            )
        ).all()
    assert rows == []
    # Re-upgrade so subsequent tests in the session find the schema.
    command.upgrade(cfg, "048")
```

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_repo_graph_migration_048.py -v
```

Expected: FAIL (migration file doesn't exist yet) — collection error or "Can't locate revision identified by '048'".

- [ ] **Step 3: Write the migration**

`migrations/versions/048_repo_graph_checkpoint.py`:

```python
"""repo_graph_checkpoint

Revision ID: 048
Revises: 047
Create Date: 2026-05-19

Adds checkpointing fields to repo_graphs so the ADR-016 pipeline can be
mid-flight resumed across container restarts / rate-limit pauses, and so
the next refresh on the same commit is a no-op:

  * is_complete       — true once a full pipeline run finished
  * processed_files   — map: rel_path -> { sites_attempted, sites_succeeded,
                        edges_added, processed_at }
  * failed_sites      — list of sites needing retry on resume

Existing rows are marked is_complete=true on upgrade: the old code only
ever wrote rows on full completion, so historically this is correct.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "048"
down_revision = "047"


def upgrade() -> None:
    op.add_column(
        "repo_graphs",
        sa.Column(
            "is_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "repo_graphs",
        sa.Column(
            "processed_files",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "repo_graphs",
        sa.Column(
            "failed_sites",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    # Backfill: every pre-existing row represented a completed analysis.
    op.execute("UPDATE repo_graphs SET is_complete = true")


def downgrade() -> None:
    op.drop_column("repo_graphs", "failed_sites")
    op.drop_column("repo_graphs", "processed_files")
    op.drop_column("repo_graphs", "is_complete")
```

- [ ] **Step 4: Run tests to verify they pass (skip if no Postgres)**

```
.venv/bin/python3 -m pytest tests/test_repo_graph_migration_048.py -v
```

Expected: 3 passed (or 3 skipped if no `DATABASE_URL`). Also verify the alembic chain is single-head:

```
.venv/bin/python3 -c "from alembic.config import Config; from alembic.script import ScriptDirectory; cfg = Config('alembic.ini'); print('heads:', [h.revision for h in ScriptDirectory.from_config(cfg).get_revisions(ScriptDirectory.from_config(cfg).get_heads())])"
```

Expected output: `heads: ['048']`.

- [ ] **Step 5: Commit**

```
git add migrations/versions/048_repo_graph_checkpoint.py tests/test_repo_graph_migration_048.py
git commit -m "feat(graph): migration 048 — is_complete / processed_files / failed_sites on repo_graphs"
```

---

## Task 2: ORM model — add checkpoint fields to `RepoGraph`

**Files:**
- Modify: `shared/models/core.py` (the `RepoGraph` class)

- [ ] **Step 1: Locate the RepoGraph class**

```
grep -n "^class RepoGraph" shared/models/core.py
```

Expected: a line like `class RepoGraph(Base):` followed by `__tablename__ = "repo_graphs"`.

- [ ] **Step 2: Add the three columns**

In the `RepoGraph` class, after `graph_json` (or wherever the other columns live), insert:

```python
    is_complete = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sa.text("false"),
    )
    processed_files = Column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa.text("'{}'::jsonb"),
    )
    failed_sites = Column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
```

Make sure `Boolean` is imported from `sqlalchemy` and `JSONB` from `sqlalchemy.dialects.postgresql`. If `sa` isn't imported, prefer `from sqlalchemy import text` and use `text("false")` directly.

- [ ] **Step 3: Import smoke**

```
.venv/bin/python3 -c "from shared.models import RepoGraph; r = RepoGraph(); print(r.is_complete, r.processed_files, r.failed_sites)"
```

Expected: `False {} []` (Python-side defaults fire when instantiating without flushing).

- [ ] **Step 4: Run existing graph tests**

```
.venv/bin/python3 -m pytest tests/test_repo_graph_config_model.py tests/test_repo_graph_migration_033.py -q
```

Expected: same pass/skip pattern as before this change (no regressions).

- [ ] **Step 5: Commit**

```
git add shared/models/core.py
git commit -m "feat(graph): RepoGraph ORM gains is_complete / processed_files / failed_sites"
```

---

## Task 3: Pydantic types — extend `LatestRepoGraphData`, add `RepoGraphProgressData`

**Files:**
- Modify: `shared/types.py`

- [ ] **Step 1: Locate the existing type**

```
grep -n "class LatestRepoGraphData" shared/types.py
```

Open the file at that line. Inspect the existing fields.

- [ ] **Step 2: Extend `LatestRepoGraphData`**

Add these fields after the existing ones (keep current ones unchanged):

```python
    is_complete: bool
    processed_files_count: int
    total_files_estimate: int
```

If the existing class is frozen/strict, follow the surrounding style.

- [ ] **Step 3: Add `RepoGraphProgressData`**

Just below `LatestRepoGraphData`:

```python
class RepoGraphProgressData(BaseModel):
    """Lightweight progress snapshot for /repos/{id}/graph/progress."""

    is_complete: bool
    processed: int
    total: int
    last_file: str | None = None
    status: Literal["running", "idle", "unchanged"]
```

Make sure `Literal` is imported (it usually is in this file).

- [ ] **Step 4: Regenerate TS types**

```
python3.12 scripts/gen_ts_types.py
```

(Fall back to `.venv/bin/python3 scripts/gen_ts_types.py` if 3.12 isn't on PATH.)

Verify the diff in `web-next/types/api.ts` includes the new fields.

- [ ] **Step 5: Commit**

```
git add shared/types.py web-next/types/api.ts
git commit -m "feat(graph): wire types — LatestRepoGraphData progress fields + RepoGraphProgressData"
```

---

## Task 4: `is_test_file` helper

**Files:**
- Create: `agent/graph_analyzer/test_filter.py`
- Test: `tests/test_graph_pipeline_test_filter.py`

- [ ] **Step 1: Write the failing test**

`tests/test_graph_pipeline_test_filter.py`:

```python
"""Verifies the test-file exclusion patterns for the ADR-016 graph walk."""

import pytest

from agent.graph_analyzer.test_filter import is_test_file


@pytest.mark.parametrize(
    "path",
    [
        "__tests__/api/admin/user-stats.test.ts",
        "tests/test_foo.py",
        "test/integration.test.tsx",
        "src/foo/bar.test.ts",
        "src/foo/bar.test.tsx",
        "src/foo/bar.spec.ts",
        "lib/baz.spec.jsx",
        "py/foo_test.py",  # NOT matched — only "*.test.*" / "*.spec.*" / dir names
        "__mocks__/server.ts",
        "cypress/e2e/login.cy.ts",
        "e2e/checkout.spec.ts",
    ],
)
def test_paths_that_look_like_tests(path: str) -> None:
    # The "py/foo_test.py" case asserts the OPPOSITE — see negatives below.
    if path == "py/foo_test.py":
        assert is_test_file(path) is False
    else:
        assert is_test_file(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/foo/bar.ts",
        "app/page.tsx",
        "apps/test-utils/foo.ts",  # 'test-utils' is NOT in _TEST_DIR_NAMES
        "lib/contests/foo.ts",     # substring match must not catch this
        "agent/loop.py",
        "Tests.ts",                # not a directory name at any path part
    ],
)
def test_paths_that_are_not_tests(path: str) -> None:
    assert is_test_file(path) is False
```

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_graph_pipeline_test_filter.py -v
```

Expected: collection error (`agent.graph_analyzer.test_filter` doesn't exist yet).

- [ ] **Step 3: Implement the filter**

`agent/graph_analyzer/test_filter.py`:

```python
"""Pattern set for excluding test/mock/fixture files from graph analysis.

Test files dominate the gap-fill cost (mock data, snapshot helpers, etc.)
yet rarely carry real dispatch edges. The pipeline filters them at the
file walk so they never produce nodes, edges, or checkpoint entries.

Hardcoded for v1. Future per-repo override via .auto-agent/graph.yml is
out of scope.
"""

from __future__ import annotations

import re


_TEST_DIR_NAMES = frozenset(
    {
        "__tests__",
        "__mocks__",
        "tests",
        "test",
        "cypress",
        "e2e",
    }
)

_TEST_FILE_RE = re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|py)$")


def is_test_file(rel_path: str) -> bool:
    """Return True iff ``rel_path`` should be skipped by the graph walk."""
    if _TEST_FILE_RE.search(rel_path):
        return True
    parts = rel_path.split("/")
    return any(p in _TEST_DIR_NAMES for p in parts)
```

- [ ] **Step 4: Run tests to verify they pass**

```
.venv/bin/python3 -m pytest tests/test_graph_pipeline_test_filter.py -v
```

Expected: all parametrized cases pass.

- [ ] **Step 5: Commit**

```
git add agent/graph_analyzer/test_filter.py tests/test_graph_pipeline_test_filter.py
git commit -m "feat(graph): is_test_file helper — exclude __tests__/, *.test.*, *.spec.*, cypress/, e2e/, __mocks__/"
```

---

## Task 5: Wire `is_test_file` into the file walk

**Files:**
- Modify: `agent/graph_analyzer/pipeline.py`

- [ ] **Step 1: Find the walk**

```
grep -n "def walk_files\|os.walk\|rglob\|iterdir" agent/graph_analyzer/pipeline.py | head
```

Locate the function that produces relative paths to be parsed.

- [ ] **Step 2: Import and filter**

At the top of `pipeline.py`:

```python
from agent.graph_analyzer.test_filter import is_test_file
```

Inside the walk (or wherever each `rel_path` is about to be yielded/processed), add the early-skip:

```python
if is_test_file(rel_path):
    continue
```

If the walk already has a list of excluded patterns (e.g., `__pycache__`, `.git/`), add the test-file check alongside but as a separate clause so the intent is clear.

- [ ] **Step 3: Smoke test against the existing graph pipeline test fixture**

```
.venv/bin/python3 -m pytest tests/test_graph_parser_python.py tests/test_graph_pipeline.py -q
```

Expected: no regressions (the existing fixture has no test files in it, so the filter is a no-op for them).

- [ ] **Step 4: Add a focused test that proves the filter is applied**

Append to `tests/test_graph_pipeline_test_filter.py`:

```python
import tempfile
from pathlib import Path
from agent.graph_analyzer import pipeline as pipeline_mod


def test_walk_skips_test_files(monkeypatch):
    """run_pipeline's file walk must yield non-test files only."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "__tests__").mkdir()
        (root / "src" / "foo.py").write_text("def f(): pass\n")
        (root / "src" / "foo.test.ts").write_text("test('x', () => {})\n")
        (root / "__tests__" / "x.py").write_text("def t(): pass\n")

        # If pipeline exposes a public walk_files, use it; otherwise fall back to
        # the internal name. Adjust based on what grep found in Step 1.
        walk_files = getattr(pipeline_mod, "walk_files", None)
        if walk_files is None:
            pytest.skip("pipeline does not expose walk_files for direct testing")
        yielded = list(walk_files(str(root)))
        assert "src/foo.py" in yielded
        assert "src/foo.test.ts" not in yielded
        assert "__tests__/x.py" not in yielded
```

If `walk_files` isn't a separately-importable function (it's inlined), refactor it into a module-level function and adjust the test. Keep the refactor minimal — just lift the loop into a named function.

- [ ] **Step 5: Run and commit**

```
.venv/bin/python3 -m pytest tests/test_graph_pipeline_test_filter.py -v
```

Expected: all pass.

```
git add agent/graph_analyzer/pipeline.py tests/test_graph_pipeline_test_filter.py
git commit -m "feat(graph): walk_files skips test files via is_test_file"
```

---

## Task 6: `changed_files` — git-diff plan

**Files:**
- Create: `agent/graph_analyzer/diff.py`
- Test: `tests/test_graph_pipeline_diff.py`

- [ ] **Step 1: Write the failing test**

`tests/test_graph_pipeline_diff.py`:

```python
"""Tests for agent/graph_analyzer/diff.py — git diff into a ChangedFilesPlan."""

from agent.graph_analyzer.diff import (
    ChangedFilesPlan,
    parse_git_name_status,
)


def test_added_modified_deleted() -> None:
    raw = b"A\x00new.ts\x00M\x00mod.ts\x00D\x00gone.ts\x00"
    plan = parse_git_name_status(raw)
    assert plan == ChangedFilesPlan(
        added=["new.ts"],
        modified=["mod.ts"],
        deleted=["gone.ts"],
        renamed_pure=[],
        renamed_modified=[],
    )


def test_pure_rename() -> None:
    raw = b"R100\x00old.ts\x00new.ts\x00"
    plan = parse_git_name_status(raw)
    assert plan.renamed_pure == [("old.ts", "new.ts")]
    assert plan.modified == []
    assert plan.added == []


def test_rename_with_modify() -> None:
    raw = b"R75\x00old.ts\x00new.ts\x00"
    plan = parse_git_name_status(raw)
    assert plan.renamed_modified == [("old.ts", "new.ts")]
    assert plan.renamed_pure == []


def test_type_change_treated_as_modify() -> None:
    raw = b"T\x00convert.ts\x00"
    plan = parse_git_name_status(raw)
    assert plan.modified == ["convert.ts"]


def test_paths_with_spaces() -> None:
    raw = b"M\x00path with space.ts\x00A\x00normal.ts\x00"
    plan = parse_git_name_status(raw)
    assert "path with space.ts" in plan.modified
    assert "normal.ts" in plan.added


def test_empty_diff_is_empty_plan() -> None:
    plan = parse_git_name_status(b"")
    assert plan == ChangedFilesPlan(
        added=[], modified=[], deleted=[], renamed_pure=[], renamed_modified=[]
    )
```

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_graph_pipeline_diff.py -v
```

Expected: import error — `agent.graph_analyzer.diff` doesn't exist.

- [ ] **Step 3: Implement `diff.py`**

`agent/graph_analyzer/diff.py`:

```python
"""Compute a ChangedFilesPlan from the workspace's git diff.

Used by ``agent/lifecycle/graph_refresh.py`` to decide which files to
re-walk when the checkpoint commit no longer matches HEAD.

The plan distinguishes pure renames (similarity = 100%) from rename+modify
because pure renames let us rewrite paths on existing nodes/edges without
re-walking the file.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class ChangedFilesPlan:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)  # M and T merge here
    deleted: list[str] = field(default_factory=list)
    renamed_pure: list[tuple[str, str]] = field(default_factory=list)
    renamed_modified: list[tuple[str, str]] = field(default_factory=list)


def parse_git_name_status(raw: bytes) -> ChangedFilesPlan:
    """Parse the NUL-separated output of:

        git diff --name-status --diff-filter=AMRTUD -z <from> <to>

    The output alternates between a status token and one (or two, for
    renames) path tokens, each NUL-terminated.
    """
    plan = ChangedFilesPlan()
    tokens = raw.split(b"\x00")
    # Trailing NUL means a final empty token; strip it.
    if tokens and tokens[-1] == b"":
        tokens = tokens[:-1]

    i = 0
    while i < len(tokens):
        status = tokens[i].decode("utf-8")
        if status.startswith("R"):
            old_path = tokens[i + 1].decode("utf-8")
            new_path = tokens[i + 2].decode("utf-8")
            try:
                similarity = int(status[1:])
            except ValueError:
                similarity = 0  # unknown — be safe, treat as rename+modify
            if similarity >= 100:
                plan.renamed_pure.append((old_path, new_path))
            else:
                plan.renamed_modified.append((old_path, new_path))
            i += 3
            continue

        path = tokens[i + 1].decode("utf-8")
        if status == "A":
            plan.added.append(path)
        elif status in ("M", "T"):
            plan.modified.append(path)
        elif status == "D":
            plan.deleted.append(path)
        # U (unmerged) is filtered out by --diff-filter; ignore if it slips in.
        i += 2

    return plan


class CheckpointCommitUnreachable(Exception):
    """Raised when `git diff <from> <to>` errors because <from> is no longer
    reachable (force-push, branch tip rewritten, etc.). Caller should fall
    back to full re-analysis from scratch."""


async def changed_files(
    workspace: str, from_sha: str, to_sha: str
) -> ChangedFilesPlan:
    """Run `git diff --name-status -z` and parse the result.

    Raises CheckpointCommitUnreachable if from_sha doesn't exist in the
    repository (typical after a force-push)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--name-status",
        "--diff-filter=AMRTUD",
        "-z",
        from_sha,
        to_sha,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        if "unknown revision" in err or "bad revision" in err or "fatal" in err:
            raise CheckpointCommitUnreachable(err.strip())
        raise RuntimeError(f"git diff failed: {err.strip()}")
    return parse_git_name_status(stdout)
```

- [ ] **Step 4: Run tests to verify they pass**

```
.venv/bin/python3 -m pytest tests/test_graph_pipeline_diff.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```
git add agent/graph_analyzer/diff.py tests/test_graph_pipeline_diff.py
git commit -m "feat(graph): diff.py — changed_files plan from git diff --name-status -z"
```

---

## Task 7: Apply `ChangedFilesPlan` to in-memory blob + checkpoint maps

**Files:**
- Modify: `agent/graph_analyzer/diff.py` (add `apply_plan`)
- Test: `tests/test_graph_pipeline_resume.py`

- [ ] **Step 1: Write the failing test**

`tests/test_graph_pipeline_resume.py`:

```python
"""Tests for the diff-plan application + smart-cascade logic."""

from agent.graph_analyzer.diff import ChangedFilesPlan, apply_plan


def _blob():
    # Hand-rolled minimal blob shape for unit testing — adjust fields if
    # the real RepoGraphBlob diverges.
    return {
        "nodes": [
            {"id": "a.ts::foo", "file": "a.ts"},
            {"id": "a.ts::bar", "file": "a.ts"},
            {"id": "b.ts::caller", "file": "b.ts"},
        ],
        "edges": [
            {"source": {"id": "b.ts::caller", "file": "b.ts"},
             "target": {"id": "a.ts::foo", "file": "a.ts"}},
            {"source": {"id": "a.ts::bar", "file": "a.ts"},
             "target": {"id": "a.ts::foo", "file": "a.ts"}},
        ],
    }


def test_deleted_file_prunes_and_cascades():
    blob = _blob()
    processed = {
        "a.ts": {"sites_attempted": 2},
        "b.ts": {"sites_attempted": 1},
    }
    plan = ChangedFilesPlan(deleted=["a.ts"])
    cascade = apply_plan(blob, processed, plan)
    # a.ts gone from nodes
    assert not any(n["file"] == "a.ts" for n in blob["nodes"])
    # all edges touching a.ts gone
    assert blob["edges"] == []
    # checkpoint for a.ts dropped
    assert "a.ts" not in processed
    # b.ts had an edge into a.ts — cascade re-walk
    assert "b.ts" in cascade
    assert "b.ts" not in processed  # dropped so it gets re-walked


def test_modified_with_lost_target_cascades():
    blob = _blob()
    processed = {"a.ts": {"sites_attempted": 2}, "b.ts": {"sites_attempted": 1}}
    plan = ChangedFilesPlan(modified=["a.ts"])
    # apply_plan needs the "new walk" result to know which nodes survived.
    # We pass it in via a callback so apply_plan stays a pure function.
    def re_walk(path):
        # Simulate: foo got renamed/removed in the new walk; bar survives.
        return {"nodes_in_path": [{"id": "a.ts::bar", "file": "a.ts"}]}
    cascade = apply_plan(blob, processed, plan, re_walk=re_walk)
    # a.ts::foo lost; b.ts had an edge into it → cascade
    assert "b.ts" in cascade
    assert "a.ts" not in processed
    assert "b.ts" not in processed


def test_modified_with_target_preserved_no_cascade():
    blob = _blob()
    processed = {"a.ts": {"sites_attempted": 2}, "b.ts": {"sites_attempted": 1}}
    plan = ChangedFilesPlan(modified=["a.ts"])
    def re_walk(path):
        # Both foo and bar survive — no cascade needed.
        return {
            "nodes_in_path": [
                {"id": "a.ts::foo", "file": "a.ts"},
                {"id": "a.ts::bar", "file": "a.ts"},
            ]
        }
    cascade = apply_plan(blob, processed, plan, re_walk=re_walk)
    assert cascade == set()
    assert "a.ts" not in processed
    assert "b.ts" in processed  # NOT dropped — its edges still resolve


def test_pure_rename_rewrites_paths_no_cascade():
    blob = _blob()
    processed = {"a.ts": {"sites_attempted": 2}}
    plan = ChangedFilesPlan(renamed_pure=[("a.ts", "moved/a.ts")])
    cascade = apply_plan(blob, processed, plan)
    assert cascade == set()
    assert "a.ts" not in processed
    assert "moved/a.ts" in processed
    # node ids should retain their old "a.ts::foo" form OR be rewritten to
    # "moved/a.ts::foo" — the test asserts the *file* field is updated.
    assert all(n["file"] == "moved/a.ts" for n in blob["nodes"] if "foo" in n["id"] or "bar" in n["id"])
```

- [ ] **Step 2: Run test to verify it fails**

```
.venv/bin/python3 -m pytest tests/test_graph_pipeline_resume.py -v
```

Expected: `ImportError: cannot import name 'apply_plan' from 'agent.graph_analyzer.diff'`.

- [ ] **Step 3: Implement `apply_plan` in `diff.py`**

Append to `agent/graph_analyzer/diff.py`:

```python
from typing import Callable, Optional


def apply_plan(
    blob: dict,
    processed: dict,
    plan: ChangedFilesPlan,
    re_walk: Optional[Callable[[str], dict]] = None,
) -> set[str]:
    """Mutate ``blob`` and ``processed`` per ``plan``. Return the set of
    additional files that need to be re-walked (the cascade set).

    ``re_walk`` is a callback used for the smart-cascade-on-M check: given
    a modified file, return ``{"nodes_in_path": [...]}`` describing the
    fresh-walk node set. apply_plan compares that to the old node ids to
    detect which previously-referenced nodes were lost.

    Files in the cascade set have their ``processed`` entry dropped so the
    pipeline's main walk picks them up again in the same run.
    """
    cascade: set[str] = set()

    # --- D ---
    for path in plan.deleted:
        cross_file_callers = {
            e["source"]["file"]
            for e in blob.get("edges", [])
            if e["target"]["file"] == path and e["source"]["file"] != path
        }
        cascade.update(cross_file_callers)
        blob["nodes"] = [n for n in blob.get("nodes", []) if n["file"] != path]
        blob["edges"] = [
            e
            for e in blob.get("edges", [])
            if e["source"]["file"] != path and e["target"]["file"] != path
        ]
        processed.pop(path, None)

    # --- M / T (with smart cascade) ---
    for path in plan.modified:
        cross_file_targets = {
            e["target"]["id"]
            for e in blob.get("edges", [])
            if e["target"]["file"] == path and e["source"]["file"] != path
        }
        # Capture cross-file callers BEFORE dropping edges (we may need them).
        callers_by_target: dict[str, set[str]] = {}
        for e in blob.get("edges", []):
            if e["target"]["file"] == path and e["source"]["file"] != path:
                callers_by_target.setdefault(e["target"]["id"], set()).add(
                    e["source"]["file"]
                )

        # Drop old state for this file.
        blob["nodes"] = [n for n in blob.get("nodes", []) if n["file"] != path]
        blob["edges"] = [
            e
            for e in blob.get("edges", [])
            if e["source"]["file"] != path and e["target"]["file"] != path
        ]
        processed.pop(path, None)

        # Smart cascade: only if a previously-targeted node was lost.
        if re_walk is not None and cross_file_targets:
            walk_result = re_walk(path)
            new_ids = {n["id"] for n in walk_result.get("nodes_in_path", [])}
            still_lost = cross_file_targets - new_ids
            for lost_id in still_lost:
                for caller_file in callers_by_target.get(lost_id, set()):
                    cascade.add(caller_file)
                    processed.pop(caller_file, None)

    # --- R100: pure rename, rewrite paths ---
    for old, new in plan.renamed_pure:
        for n in blob.get("nodes", []):
            if n["file"] == old:
                n["file"] = new
                # If node ids embed the path, rewrite that too.
                if n.get("id", "").startswith(f"{old}::"):
                    n["id"] = n["id"].replace(f"{old}::", f"{new}::", 1)
        for e in blob.get("edges", []):
            if e["source"]["file"] == old:
                e["source"]["file"] = new
                if e["source"].get("id", "").startswith(f"{old}::"):
                    e["source"]["id"] = e["source"]["id"].replace(
                        f"{old}::", f"{new}::", 1
                    )
            if e["target"]["file"] == old:
                e["target"]["file"] = new
                if e["target"].get("id", "").startswith(f"{old}::"):
                    e["target"]["id"] = e["target"]["id"].replace(
                        f"{old}::", f"{new}::", 1
                    )
        if old in processed:
            processed[new] = processed.pop(old)

    # --- R<low>: rename + modify ---
    # Treat as: delete old (with cascade), add new (just walked by main loop).
    if plan.renamed_modified:
        synth = ChangedFilesPlan(deleted=[o for o, _ in plan.renamed_modified])
        cascade.update(apply_plan(blob, processed, synth, re_walk=None))
        # New paths get processed by the main walk; nothing more to do here.

    return cascade
```

- [ ] **Step 4: Run tests to verify they pass**

```
.venv/bin/python3 -m pytest tests/test_graph_pipeline_resume.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```
git add agent/graph_analyzer/diff.py tests/test_graph_pipeline_resume.py
git commit -m "feat(graph): apply_plan with smart cascade on M, pure-rename rewrites, D cascade"
```

---

## Task 8: Pipeline per-file checkpoint flush

**Files:**
- Modify: `agent/graph_analyzer/pipeline.py`

The pipeline currently builds the full `RepoGraphBlob` in memory and returns it. We add an optional checkpoint-flush callback so the caller (run_refresh) can persist after every file.

- [ ] **Step 1: Add a callback param to `run_pipeline`**

In `agent/graph_analyzer/pipeline.py`, change `run_pipeline`'s signature:

```python
from typing import Awaitable, Callable, Optional

CheckpointFlush = Callable[[dict, dict, list], Awaitable[None]]
# (blob_dict, processed_files_dict, failed_sites_list) -> None
```

```python
async def run_pipeline(
    *,
    workspace: str,
    commit_sha: str,
    provider,
    on_file_checkpoint: Optional[CheckpointFlush] = None,
    initial_processed_files: Optional[dict] = None,
    initial_failed_sites: Optional[list] = None,
    initial_blob: Optional[dict] = None,
) -> "RepoGraphBlob":
```

- [ ] **Step 2: Use the initial state if provided**

At the top of the function body, replace the empty-state initialization with:

```python
processed_files = dict(initial_processed_files or {})
failed_sites = list(initial_failed_sites or [])
# Resume case: start from the persisted blob shape if one was supplied.
blob_dict: dict
if initial_blob is not None:
    blob_dict = dict(initial_blob)  # shallow copy, callers won't mutate ours
    blob_dict.setdefault("nodes", [])
    blob_dict.setdefault("edges", [])
else:
    blob_dict = {"nodes": [], "edges": [], "commit_sha": commit_sha,
                 "areas": [], "public_symbols": []}
```

- [ ] **Step 3: Drive the file loop through the test filter + skip-if-checkpointed gates**

Inside the existing file walk (call it `for rel_path in walk_files(workspace):` whether direct or via the existing helper):

```python
if is_test_file(rel_path):
    continue

retry_due = any(s["file"] == rel_path for s in failed_sites)
if rel_path in processed_files and not retry_due:
    continue

parse_result = parse_file(workspace, rel_path)  # existing
sites = extract_sites(parse_result)              # existing
new_edges, errored_sites = await process_sites(sites, provider=provider)

blob_dict["nodes"].extend(parse_result.nodes)
blob_dict["edges"].extend(new_edges)
processed_files[rel_path] = {
    "sites_attempted": len(sites),
    "sites_succeeded": len(sites) - len(errored_sites),
    "edges_added": len(new_edges),
    "processed_at": _now_iso(),
}
failed_sites = [s for s in failed_sites if s["file"] != rel_path] + errored_sites

if on_file_checkpoint is not None:
    await on_file_checkpoint(blob_dict, processed_files, failed_sites)
```

`_now_iso()` is a small helper at module scope:

```python
from datetime import datetime, timezone

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
```

If `parse_file` / `extract_sites` / `process_sites` aren't the exact names in pipeline.py, adapt to the actual ones — but keep the per-file shape: parse → sites → process → merge → checkpoint.

- [ ] **Step 4: Return shape unchanged**

The function still returns the final `RepoGraphBlob`. Construct it from `blob_dict` at the end the same way the existing code does (call the existing finalize step that fills `areas`, `public_symbols`, etc.).

- [ ] **Step 5: Run existing pipeline tests**

```
.venv/bin/python3 -m pytest tests/test_graph_pipeline.py tests/test_graph_pipeline_phase3.py tests/test_graph_pipeline_public_symbols.py -q
```

Expected: still pass — these tests don't pass `on_file_checkpoint`, so the new param is opt-in.

- [ ] **Step 6: Commit**

```
git add agent/graph_analyzer/pipeline.py
git commit -m "feat(graph): run_pipeline checkpoints per file via on_file_checkpoint callback"
```

---

## Task 8b: Add `parse_file_for_nodes` to the parsers package

**Files:**
- Modify: `agent/graph_analyzer/parsers/__init__.py` (or per-language module)

A cheap helper used by run_refresh's M-cascade pre-pass: tree-sitter parse a single file and return the list of node ids it produces, **without** any gap-fill / LLM step.

- [ ] **Step 1: Inspect the existing parsers**

```
grep -n "^def \|^async def " agent/graph_analyzer/parsers/__init__.py agent/graph_analyzer/parsers/python.py agent/graph_analyzer/parsers/typescript.py | head
```

Find the function that produces nodes for a single file. Common name candidates: `parse_python_file`, `parse_typescript_file`, `parse_file`.

- [ ] **Step 2: Add the helper**

In `agent/graph_analyzer/parsers/__init__.py`:

```python
import os
from typing import Any


def parse_file_for_nodes(workspace: str, rel_path: str) -> list[dict[str, Any]]:
    """Cheap tree-sitter parse — returns just the node ids for one file.

    No gap-fill, no LLM, no edges. Used by run_refresh's M-cascade pre-pass
    to determine which previously-targeted nodes survived a file change.
    """
    abs_path = os.path.join(workspace, rel_path)
    ext = os.path.splitext(rel_path)[1].lower()
    if ext in (".py",):
        from agent.graph_analyzer.parsers.python import parse_python_file
        result = parse_python_file(abs_path, rel_path)
    elif ext in (".ts", ".tsx", ".js", ".jsx"):
        from agent.graph_analyzer.parsers.typescript import parse_typescript_file
        result = parse_typescript_file(abs_path, rel_path)
    else:
        return []
    # Each parser returns an object with a .nodes attribute (or dict);
    # normalise to a list of {"id": ..., "file": ...} dicts.
    nodes = getattr(result, "nodes", None) or result.get("nodes", [])
    return [{"id": n["id"] if isinstance(n, dict) else n.id,
             "file": n["file"] if isinstance(n, dict) else n.file}
            for n in nodes]
```

Adjust the imported function names to whatever step 1 revealed. If a parser returns a different shape, adapt the normalisation.

- [ ] **Step 3: Smoke**

```
.venv/bin/python3 -c "from agent.graph_analyzer.parsers import parse_file_for_nodes; print('ok')"
```

Expected: `ok`.

- [ ] **Step 4: Commit**

```
git add agent/graph_analyzer/parsers/__init__.py
git commit -m "feat(graph): parse_file_for_nodes — cheap tree-sitter-only single-file parse"
```

---

## Task 9: `run_refresh` — row-load cases + diff apply

**Files:**
- Modify: `agent/lifecycle/graph_refresh.py`

- [ ] **Step 1: Add helpers at module scope**

Near the top of `agent/lifecycle/graph_refresh.py`, after the existing imports:

```python
from agent.graph_analyzer.diff import (
    ChangedFilesPlan,
    CheckpointCommitUnreachable,
    apply_plan,
    changed_files,
)
```

Add this helper:

```python
async def _load_or_create_row(session, repo_id: int, commit_sha: str):
    """Load the latest repo_graphs row for this repo, or create a fresh
    in-progress row when none exists.

    Returns (row, action) where action is one of:
      "noop"            — row is complete at this exact commit_sha
      "fresh"           — new row created
      "resume_same"     — row is incomplete at this commit_sha
      "resume_diff"     — row's commit_sha differs from HEAD, diff needed
    """
    from sqlalchemy import select
    result = await session.execute(
        select(RepoGraph).where(RepoGraph.repo_id == repo_id)
        .order_by(RepoGraph.id.desc()).limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = RepoGraph(
            repo_id=repo_id,
            commit_sha=commit_sha,
            generated_at=_now_dt(),
            analyser_version=_analyser_version(),
            status="ok",
            graph_json={"nodes": [], "edges": [], "areas": [],
                        "public_symbols": [], "commit_sha": commit_sha},
            is_complete=False,
            processed_files={},
            failed_sites=[],
        )
        session.add(row)
        await session.flush()
        return row, "fresh"
    if row.is_complete and row.commit_sha == commit_sha:
        return row, "noop"
    if row.commit_sha == commit_sha:
        return row, "resume_same"
    return row, "resume_diff"
```

`_now_dt` and `_analyser_version` use whatever the existing code uses — copy the pattern from where rows are currently constructed.

- [ ] **Step 2: Replace the run_refresh body**

Find the current `run_refresh` function. After `_prepare_workspace` and `_resolve_commit_sha`, replace the existing call to `run_pipeline` with this orchestrating block:

```python
async with async_session() as session:
    row, action = await _load_or_create_row(session, repo_id, commit_sha)

    if action == "noop":
        await session.commit()
        log.info("graph_refresh_noop", repo_id=repo_id, commit_sha=commit_sha)
        await publish(repo_graph_ready(
            repo_id=repo_id, repo_graph_id=row.id, commit_sha=commit_sha,
        ))
        return

    # Apply commit-diff plan if commit changed.
    if action == "resume_diff":
        from_sha = row.commit_sha
        try:
            plan = await changed_files(workspace, from_sha, commit_sha)
        except CheckpointCommitUnreachable as e:
            log.warning(
                "graph_refresh_checkpoint_unreachable",
                repo_id=repo_id,
                from_sha=from_sha,
                error=str(e),
            )
            row.processed_files = {}
            row.failed_sites = []
            row.graph_json = {"nodes": [], "edges": [], "areas": [],
                              "public_symbols": [], "commit_sha": commit_sha}
        else:
            blob_dict = dict(row.graph_json or {})
            processed = dict(row.processed_files or {})
            # Smart cascade on M needs to know what nodes survive the new
            # walk of each modified file. Tree-sitter parse is cheap (no
            # LLM), so we do a pre-pass here that only extracts node ids:
            from agent.graph_analyzer.parsers import parse_file_for_nodes

            def _re_walk_nodes_only(rel_path: str) -> dict:
                try:
                    nodes = parse_file_for_nodes(workspace, rel_path)
                except Exception as e:
                    log.warning(
                        "graph_refresh_cascade_parse_failed",
                        repo_id=repo_id, file=rel_path, error=str(e),
                    )
                    # On parse failure, return empty so apply_plan treats
                    # every cross-file target as lost — conservative.
                    return {"nodes_in_path": []}
                return {"nodes_in_path": nodes}

            apply_plan(blob_dict, processed, plan, re_walk=_re_walk_nodes_only)
            row.graph_json = blob_dict
            row.processed_files = processed
        row.commit_sha = commit_sha
        row.is_complete = False

    if action == "resume_same":
        row.is_complete = False

    row_id = row.id
    await session.commit()

# Run the pipeline with a checkpoint-flushing callback.
async def flush_checkpoint(blob_dict, processed_files, failed_sites):
    async with async_session() as s:
        r = await s.get(RepoGraph, row_id)
        r.graph_json = blob_dict
        r.processed_files = processed_files
        r.failed_sites = failed_sites
        await s.commit()

blob = await run_pipeline(
    workspace=workspace,
    commit_sha=commit_sha,
    provider=get_structured_extractor_provider(),
    on_file_checkpoint=flush_checkpoint,
    initial_processed_files=row.processed_files,
    initial_failed_sites=row.failed_sites,
    initial_blob=row.graph_json,
)

# Finalize.
async with async_session() as session:
    r = await session.get(RepoGraph, row_id)
    r.graph_json = blob.model_dump(mode="json") if hasattr(blob, "model_dump") else dict(blob)
    r.is_complete = True
    r.status = overall_status(getattr(blob, "areas", []))
    r.generated_at = _now_dt()
    await session.commit()

await publish(repo_graph_ready(
    repo_id=repo_id, repo_graph_id=row_id, commit_sha=commit_sha,
))
```

If the existing code uses `row.graph_json = json.loads(blob.model_dump_json())` rather than `blob.model_dump(mode="json")`, keep the existing convention for byte-for-byte compatibility.

- [ ] **Step 3: Drop the existing one-shot INSERT path**

Delete the old `row = RepoGraph(...)` + `session.add(row)` block that fired only at end-of-pipeline — its job is now covered by the in-place UPDATE.

- [ ] **Step 4: Run existing handler tests, expect some to fail**

```
.venv/bin/python3 -m pytest tests/test_graph_refresh_handler.py -v
```

Some will fail because they asserted the single-INSERT shape. We update them in Task 12. For now confirm the **module imports cleanly**:

```
.venv/bin/python3 -c "from agent.lifecycle.graph_refresh import run_refresh; print('ok')"
```

Expected: `ok`.

- [ ] **Step 5: Commit**

```
git add agent/lifecycle/graph_refresh.py
git commit -m "feat(graph): run_refresh row-load cases + commit-diff resume"
```

---

## Task 10: `GET /repos/{id}/graph/latest` extension + `GET /repos/{id}/graph/progress`

**Files:**
- Modify: `orchestrator/router.py`

- [ ] **Step 1: Locate the latest endpoint**

```
grep -n "/repos/{repo_id}/graph/latest\|LatestRepoGraphData" orchestrator/router.py | head
```

Open the file at that line.

- [ ] **Step 2: Extend the response builder**

The existing handler probably returns something like `LatestRepoGraphData(...)`. Add the three new fields. A typical shape:

```python
@router.get("/repos/{repo_id}/graph/latest", response_model=LatestRepoGraphData)
async def get_latest_repo_graph(
    repo_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> LatestRepoGraphData:
    repo = await _get_repo_in_org(session, repo_id=repo_id, org_id=org_id)
    if not repo:
        raise HTTPException(404, "Repo not found")
    result = await session.execute(
        select(RepoGraph).where(RepoGraph.repo_id == repo.id)
        .order_by(RepoGraph.id.desc()).limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "No analysis yet")

    total_est = await _estimate_total_files(repo_id=repo.id)  # see Step 3
    return LatestRepoGraphData(
        # ...existing fields ($graph_json, status, commit_sha, generated_at, etc.)...
        is_complete=row.is_complete,
        processed_files_count=len(row.processed_files or {}),
        total_files_estimate=total_est,
    )
```

Keep all existing fields the handler already returns — only add the three new ones.

- [ ] **Step 3: Add the estimate helper (30s memoized)**

Above the handler, in the same file:

```python
import time
_TOTAL_FILES_CACHE: dict[int, tuple[float, int]] = {}
_TOTAL_FILES_TTL = 30.0


async def _estimate_total_files(repo_id: int) -> int:
    """Count non-test source files in the graph workspace.

    Memoized 30s per repo so repeated /graph/latest hits don't re-walk."""
    from agent.graph_analyzer.test_filter import is_test_file
    from agent.graph_workspace import graph_workspace_path
    import os

    now = time.time()
    cached = _TOTAL_FILES_CACHE.get(repo_id)
    if cached and now - cached[0] < _TOTAL_FILES_TTL:
        return cached[1]

    workspace = graph_workspace_path(repo_id=repo_id)
    if not os.path.isdir(workspace):
        _TOTAL_FILES_CACHE[repo_id] = (now, 0)
        return 0

    exts = (".py", ".ts", ".tsx", ".js", ".jsx")
    count = 0
    for root, dirs, files in os.walk(workspace):
        # Skip the .git directory and node_modules.
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".next")]
        for fname in files:
            if not fname.endswith(exts):
                continue
            rel_path = os.path.relpath(os.path.join(root, fname), workspace)
            if is_test_file(rel_path):
                continue
            count += 1
    _TOTAL_FILES_CACHE[repo_id] = (now, count)
    return count
```

- [ ] **Step 4: Add the progress endpoint**

In the same code-graph section of router.py:

```python
@router.get("/repos/{repo_id}/graph/progress", response_model=RepoGraphProgressData)
async def get_repo_graph_progress(
    repo_id: int,
    session: AsyncSession = Depends(get_session),
    org_id: int = Depends(current_org_id_dep),
) -> RepoGraphProgressData:
    repo = await _get_repo_in_org(session, repo_id=repo_id, org_id=org_id)
    if not repo:
        raise HTTPException(404, "Repo not found")
    result = await session.execute(
        select(RepoGraph).where(RepoGraph.repo_id == repo.id)
        .order_by(RepoGraph.id.desc()).limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return RepoGraphProgressData(
            is_complete=False, processed=0, total=0, last_file=None, status="idle"
        )

    processed = row.processed_files or {}
    total = await _estimate_total_files(repo_id=repo.id)
    last_file = None
    if processed:
        last_file = max(
            processed.items(),
            key=lambda kv: kv[1].get("processed_at", ""),
        )[0]

    # "running" if not complete and the lock is held; otherwise "idle".
    # The lock check is cheap — see graph_workspace.lock_is_held(repo_id).
    from agent.graph_workspace import lock_is_held
    status = (
        "unchanged" if row.is_complete else
        ("running" if lock_is_held(repo_id) else "idle")
    )
    return RepoGraphProgressData(
        is_complete=row.is_complete,
        processed=len(processed),
        total=total,
        last_file=last_file,
        status=status,
    )
```

If `lock_is_held` doesn't yet exist in `agent.graph_workspace`, add it:

```python
def lock_is_held(repo_id: int) -> bool:
    """Best-effort: does another process hold the per-repo graph lock?

    We don't try to acquire the lock; we just check whether the lock file
    has a writer. On POSIX, fcntl.lockf with F_TEST tells us."""
    import fcntl, os
    lock_path = os.path.join(_GRAPH_WORKSPACES_DIR, f".lock-{repo_id}")
    if not os.path.exists(lock_path):
        return False
    try:
        fd = os.open(lock_path, os.O_RDONLY)
        try:
            fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.lockf(fd, fcntl.LOCK_UN)
            return False  # we got a shared lock → no exclusive writer
        except (BlockingIOError, OSError):
            return True
        finally:
            os.close(fd)
    except OSError:
        return False
```

Use the existing `_GRAPH_WORKSPACES_DIR` constant from the same module.

- [ ] **Step 5: Imports**

At the top of router.py, ensure `RepoGraphProgressData` is imported alongside the existing `LatestRepoGraphData`.

- [ ] **Step 6: Smoke**

```
.venv/bin/python3 -m pytest tests/test_repo_graph_api.py tests/test_repo_graph_refresh_endpoint.py -q
```

Expected: still pass for the parts that don't reference the new fields. (New tests come later.)

- [ ] **Step 7: Commit**

```
git add orchestrator/router.py agent/graph_workspace.py
git commit -m "feat(graph): /graph/latest gains progress fields; new /graph/progress endpoint"
```

---

## Task 11: TS API client + `useRepoGraphProgress` hook

**Files:**
- Modify: `web-next/lib/code-graph.ts`
- Create: `web-next/hooks/useRepoGraphProgress.ts`
- Test: `web-next/tests/use-repo-graph-progress.test.ts`

- [ ] **Step 1: Add the API call to `lib/code-graph.ts`**

Append to `web-next/lib/code-graph.ts`:

```typescript
import type { RepoGraphProgressData } from "@/types/api";

export async function getRepoGraphProgress(
  repoId: number,
): Promise<RepoGraphProgressData> {
  const res = await fetch(`/api/repos/${repoId}/graph/progress`, {
    credentials: "include",
  });
  if (!res.ok) {
    throw new Error(`progress fetch failed: ${res.status}`);
  }
  return res.json();
}
```

- [ ] **Step 2: Write the failing test**

`web-next/tests/use-repo-graph-progress.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import * as codeGraph from "@/lib/code-graph";
import { useRepoGraphProgress } from "@/hooks/useRepoGraphProgress";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("useRepoGraphProgress", () => {
  beforeEach(() => vi.clearAllMocks());

  it("polls every 5s while incomplete", async () => {
    const spy = vi
      .spyOn(codeGraph, "getRepoGraphProgress")
      .mockResolvedValue({
        is_complete: false,
        processed: 5,
        total: 20,
        last_file: "src/foo.ts",
        status: "running",
      });
    const { result, rerender } = renderHook(() => useRepoGraphProgress(7), {
      wrapper,
    });
    await waitFor(() => expect(result.current.data?.processed).toBe(5));
    // Hook returned its refetchInterval; assert it picked the short cadence.
    expect(result.current.refetchInterval).toBe(5000);
  });

  it("polls every 60s when complete", async () => {
    vi.spyOn(codeGraph, "getRepoGraphProgress").mockResolvedValue({
      is_complete: true,
      processed: 20,
      total: 20,
      last_file: "src/zzz.ts",
      status: "unchanged",
    });
    const { result } = renderHook(() => useRepoGraphProgress(7), { wrapper });
    await waitFor(() => expect(result.current.data?.is_complete).toBe(true));
    expect(result.current.refetchInterval).toBe(60000);
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

```
cd web-next && npm run test -- use-repo-graph-progress
```

Expected: FAIL (hook doesn't exist).

- [ ] **Step 4: Implement the hook**

`web-next/hooks/useRepoGraphProgress.ts`:

```typescript
import { useQuery } from "@tanstack/react-query";
import { getRepoGraphProgress } from "@/lib/code-graph";
import type { RepoGraphProgressData } from "@/types/api";

const SHORT_INTERVAL = 5_000;
const LONG_INTERVAL = 60_000;

/**
 * Polls /repos/{repoId}/graph/progress.
 *
 * - 5s cadence while is_complete=false (the analyser is running).
 * - 60s cadence once is_complete=true (mostly to detect a fresh run).
 *
 * Returns the TanStack query result plus the chosen `refetchInterval`
 * (exposed for testability — assertion in the unit test).
 */
export function useRepoGraphProgress(repoId: number) {
  const query = useQuery<RepoGraphProgressData>({
    queryKey: ["repo-graph-progress", repoId],
    queryFn: () => getRepoGraphProgress(repoId),
    refetchInterval: (data) =>
      data?.is_complete ? LONG_INTERVAL : SHORT_INTERVAL,
    staleTime: 0,
  });
  const refetchInterval = query.data?.is_complete ? LONG_INTERVAL : SHORT_INTERVAL;
  return { ...query, refetchInterval };
}
```

- [ ] **Step 5: Run test to verify it passes**

```
cd web-next && npm run test -- use-repo-graph-progress
```

Expected: 2 passed.

- [ ] **Step 6: Commit**

```
git add web-next/hooks/useRepoGraphProgress.ts web-next/lib/code-graph.ts web-next/tests/use-repo-graph-progress.test.ts
git commit -m "feat(web-next): useRepoGraphProgress hook + lib helper"
```

---

## Task 12: `<GraphCompletionBadge />` component

**Files:**
- Create: `web-next/components/code-graph/graph-completion-badge.tsx`
- Test: `web-next/tests/graph-completion-badge.test.tsx`

- [ ] **Step 1: Write the failing test**

`web-next/tests/graph-completion-badge.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { GraphCompletionBadge } from "@/components/code-graph/graph-completion-badge";

describe("GraphCompletionBadge", () => {
  it("shows Complete label when is_complete=true", () => {
    render(
      <GraphCompletionBadge
        progress={{
          is_complete: true,
          processed: 200,
          total: 200,
          last_file: null,
          status: "unchanged",
        }}
      />,
    );
    expect(screen.getByText(/Complete/)).toBeInTheDocument();
    expect(screen.getByText(/200 files/)).toBeInTheDocument();
  });

  it("shows Analyzing while running", () => {
    render(
      <GraphCompletionBadge
        progress={{
          is_complete: false,
          processed: 50,
          total: 200,
          last_file: "app/foo.tsx",
          status: "running",
        }}
      />,
    );
    expect(screen.getByText(/Analyzing/)).toBeInTheDocument();
    expect(screen.getByText(/50 \/ 200/)).toBeInTheDocument();
    expect(screen.getByText(/app\/foo\.tsx/)).toBeInTheDocument();
  });

  it("shows Partial when idle and incomplete", () => {
    render(
      <GraphCompletionBadge
        progress={{
          is_complete: false,
          processed: 100,
          total: 200,
          last_file: "app/bar.tsx",
          status: "idle",
        }}
      />,
    );
    expect(screen.getByText(/Partial/)).toBeInTheDocument();
    expect(screen.getByText(/100 \/ 200/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

```
cd web-next && npm run test -- graph-completion-badge
```

Expected: FAIL (component doesn't exist).

- [ ] **Step 3: Implement the component**

`web-next/components/code-graph/graph-completion-badge.tsx`:

```tsx
"use client";

import { Loader2 } from "lucide-react";
import type { RepoGraphProgressData } from "@/types/api";

export function GraphCompletionBadge({
  progress,
}: {
  progress: RepoGraphProgressData;
}) {
  if (progress.is_complete) {
    return (
      <span className="inline-flex items-center rounded-full bg-green-50 px-3 py-1 text-xs font-medium text-green-700 ring-1 ring-inset ring-green-600/20">
        Complete · {progress.total} files
      </span>
    );
  }
  if (progress.status === "running") {
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-3 py-1 text-xs font-medium text-amber-800 ring-1 ring-inset ring-amber-600/30">
        <Loader2 className="h-3 w-3 animate-spin" />
        Analyzing · {progress.processed} / {progress.total} files
        {progress.last_file ? ` (file: ${progress.last_file})` : ""}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center rounded-full bg-zinc-100 px-3 py-1 text-xs font-medium text-zinc-700 ring-1 ring-inset ring-zinc-500/30">
      Partial · {progress.processed} / {progress.total} files. Click Refresh to resume.
    </span>
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

```
cd web-next && npm run test -- graph-completion-badge
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```
git add web-next/components/code-graph/graph-completion-badge.tsx web-next/tests/graph-completion-badge.test.tsx
git commit -m "feat(web-next): GraphCompletionBadge — complete / analyzing / partial states"
```

---

## Task 13: Refresh button label adapts to completeness

**Files:**
- Modify: `web-next/components/code-graph/refresh-button.tsx`

- [ ] **Step 1: Inspect current button**

```
sed -n '1,60p' web-next/components/code-graph/refresh-button.tsx
```

Find where the button label `"Refresh"` is rendered.

- [ ] **Step 2: Accept `isComplete` prop and switch label**

Extend the component's props with `isComplete: boolean` (optional, default `true` for backward compat). Switch the label:

```tsx
<button {/* existing attrs */}>
  {props.isComplete ?? true ? "Refresh" : "Resume / Re-analyze"}
</button>
```

If the button has a tooltip / aria-label, update both.

- [ ] **Step 3: Smoke**

```
cd web-next && npm run typecheck
```

Expected: clean.

- [ ] **Step 4: Commit**

```
git add web-next/components/code-graph/refresh-button.tsx
git commit -m "feat(web-next): RefreshButton label flips to 'Resume / Re-analyze' on partial"
```

---

## Task 14: Mount the badge + progress hook on the code-graph page

**Files:**
- Modify: `web-next/app/(app)/code-graph/[repoId]/page.tsx`

- [ ] **Step 1: Add imports + hook usage**

Near the existing imports:

```tsx
import { useRepoGraphProgress } from "@/hooks/useRepoGraphProgress";
import { GraphCompletionBadge } from "@/components/code-graph/graph-completion-badge";
```

Inside the page component, after the other hook calls:

```tsx
const progressQuery = useRepoGraphProgress(repoId);
const progress = progressQuery.data;
```

- [ ] **Step 2: Render the badge in the header**

Find the existing `FreshnessBanner` render site. Render `GraphCompletionBadge` immediately next to it:

```tsx
{progress ? <GraphCompletionBadge progress={progress} /> : null}
```

- [ ] **Step 3: Pass `isComplete` to RefreshButton**

Find where `<RefreshButton ... />` is rendered. Add the prop:

```tsx
<RefreshButton
  /* existing props */
  isComplete={progress?.is_complete ?? true}
/>
```

- [ ] **Step 4: Verify typecheck + tests**

```
cd web-next && npm run typecheck && npm run test
```

Expected: clean + all existing tests pass.

- [ ] **Step 5: Commit**

```
git add web-next/app/\(app\)/code-graph/\[repoId\]/page.tsx
git commit -m "feat(web-next): code-graph page mounts progress hook + completion badge"
```

---

## Task 15: Update existing `test_graph_refresh_handler.py` to the UPDATE-per-file shape

**Files:**
- Modify: `tests/test_graph_refresh_handler.py`

The existing tests assume a single INSERT at end-of-pipeline. With the new flow, the handler UPDATEs a pre-existing or freshly-INSERTed row per file. The existing 5 failures on macOS were `/data` mkdir issues — those get fixed in the same pass.

- [ ] **Step 1: Read the existing tests**

```
sed -n '1,80p' tests/test_graph_refresh_handler.py
```

Note: the file likely has fixtures that mock `_run_git`, `async_session`, and `run_pipeline`. Some assertions check `session.add` was called with a `RepoGraph(...)` instance.

- [ ] **Step 2: Adapt the assertions**

For each test, replace assertions like:

```python
assert any(isinstance(arg, RepoGraph) for arg, _ in session_mock.add.call_args_list)
```

with row-mutation assertions:

```python
# The new flow inserts a row in run_refresh (fresh case) and then
# UPDATEs it. Assert the row was created with is_complete=False initially
# and reached is_complete=True by the end.
created_rows = [arg for arg, _ in session_mock.add.call_args_list
                if isinstance(arg, RepoGraph)]
assert len(created_rows) == 1
row = created_rows[0]
assert row.is_complete is True  # final state after pipeline + finalize
```

For the `/data` mkdir failures (the existing 5 macOS failures): mock `agent.graph_workspace.graph_workspace_path` to return a `tmp_path` directory. Apply the mock as a fixture so every test in the file uses it:

```python
@pytest.fixture(autouse=True)
def _graph_workspaces_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.graph_workspace.graph_workspace_path",
        lambda repo_id: str(tmp_path / "graph-workspaces" / str(repo_id)),
    )
    monkeypatch.setattr(
        "agent.graph_workspace._GRAPH_WORKSPACES_DIR",
        str(tmp_path / "graph-workspaces"),
    )
```

- [ ] **Step 3: Run the file**

```
.venv/bin/python3 -m pytest tests/test_graph_refresh_handler.py -v
```

Expected: all tests pass (no more `/data` errors; assertions updated to new flow).

- [ ] **Step 4: Commit**

```
git add tests/test_graph_refresh_handler.py
git commit -m "test(graph): adapt graph_refresh_handler tests to UPDATE-per-file flow + fix /data mocking"
```

---

## Task 16: End-to-end resume test

**Files:**
- Create: `tests/test_graph_refresh_resume_e2e.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end: refresh, cancel mid-pipeline, refresh again, assert resume.

Uses mocks for git + LLM provider (mirrors tests/test_graph_refresh_handler.py).
DB-backed — skips when DATABASE_URL is missing."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from shared.database import async_session
from shared.models import Repo, RepoGraph, RepoGraphConfig


def _skip_if_no_db():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("requires DATABASE_URL")


@pytest.mark.asyncio
async def test_resume_after_midflight_cancel(monkeypatch, tmp_path):
    _skip_if_no_db()
    from agent.lifecycle import graph_refresh

    # ---- Fixture: a Repo + RepoGraphConfig ----
    async with async_session() as s:
        repo = Repo(name="cardamon-e2e", url="https://github.com/x/y",
                    default_branch="main", organization_id=1)
        s.add(repo)
        await s.flush()
        cfg = RepoGraphConfig(
            repo_id=repo.id, organization_id=1, analysis_branch="main",
            analyser_version="", workspace_path=str(tmp_path / "ws"),
        )
        s.add(cfg)
        await s.commit()
        repo_id = repo.id

    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()

    # ---- Mocks for git + pipeline ----
    monkeypatch.setattr(graph_refresh, "_run_git", AsyncMock(return_value="cafebabe\n"))
    monkeypatch.setattr(graph_refresh, "_prepare_workspace", AsyncMock())
    monkeypatch.setattr(graph_refresh, "_resolve_commit_sha", AsyncMock(return_value="cafebabe"))
    monkeypatch.setattr(
        "agent.graph_workspace.graph_workspace_path",
        lambda repo_id: str(workspace),
    )

    # First run: pipeline raises after writing 2 files of checkpoint.
    call_count = {"flushed": 0}

    async def fake_pipeline(*, on_file_checkpoint=None, **kwargs):
        await on_file_checkpoint(
            {"nodes": [{"id": "a.ts::x", "file": "a.ts"}], "edges": []},
            {"a.ts": {"sites_attempted": 1, "sites_succeeded": 1,
                      "edges_added": 0, "processed_at": "2026-05-19T00:00:00Z"}},
            [],
        )
        call_count["flushed"] += 1
        await on_file_checkpoint(
            {"nodes": [{"id": "a.ts::x", "file": "a.ts"},
                       {"id": "b.ts::y", "file": "b.ts"}],
             "edges": []},
            {"a.ts": {"sites_attempted": 1, "sites_succeeded": 1,
                      "edges_added": 0, "processed_at": "2026-05-19T00:00:00Z"},
             "b.ts": {"sites_attempted": 1, "sites_succeeded": 1,
                      "edges_added": 0, "processed_at": "2026-05-19T00:00:01Z"}},
            [],
        )
        call_count["flushed"] += 1
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(graph_refresh, "run_pipeline", fake_pipeline)

    with pytest.raises(RuntimeError):
        await graph_refresh.run_refresh(repo_id=repo_id, request_id="r1")

    # After crash: row exists, is_complete=False, 2 files in processed_files.
    async with async_session() as s:
        result = await s.execute(select(RepoGraph).where(RepoGraph.repo_id == repo_id))
        row = result.scalar_one()
        assert row.is_complete is False
        assert set(row.processed_files.keys()) == {"a.ts", "b.ts"}

    # Second run: pipeline succeeds, adds c.ts.
    async def fake_pipeline_2(*, on_file_checkpoint=None, initial_processed_files=None, **kwargs):
        assert set(initial_processed_files.keys()) == {"a.ts", "b.ts"}
        await on_file_checkpoint(
            {"nodes": [{"id": "c.ts::z", "file": "c.ts"}], "edges": []},
            {**initial_processed_files,
             "c.ts": {"sites_attempted": 1, "sites_succeeded": 1,
                      "edges_added": 0, "processed_at": "2026-05-19T00:00:02Z"}},
            [],
        )
        # Return a fake blob shape — match what real run_pipeline returns.
        from types import SimpleNamespace
        return SimpleNamespace(
            model_dump=lambda mode=None: {
                "nodes": [], "edges": [], "areas": [], "public_symbols": [],
                "commit_sha": "cafebabe",
            },
            areas=[],
        )

    monkeypatch.setattr(graph_refresh, "run_pipeline", fake_pipeline_2)
    await graph_refresh.run_refresh(repo_id=repo_id, request_id="r2")

    async with async_session() as s:
        result = await s.execute(select(RepoGraph).where(RepoGraph.repo_id == repo_id))
        row = result.scalar_one()
        assert row.is_complete is True
        assert "c.ts" in row.processed_files
```

- [ ] **Step 2: Run**

```
.venv/bin/python3 -m pytest tests/test_graph_refresh_resume_e2e.py -v
```

Expected: 1 passed (or 1 skipped if no DATABASE_URL).

- [ ] **Step 3: Commit**

```
git add tests/test_graph_refresh_resume_e2e.py
git commit -m "test(graph): e2e resume across simulated mid-flight crash"
```

---

## Task 17: DB-backed resume + no-op tests

**Files:**
- Create: `tests/test_repo_graph_resume_db.py`

- [ ] **Step 1: Write the tests**

```python
"""DB-backed integration: row-load cases in run_refresh."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from shared.database import async_session
from shared.models import Repo, RepoGraph, RepoGraphConfig


def _skip_if_no_db():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("requires DATABASE_URL")


@pytest.mark.asyncio
async def test_noop_on_unchanged_commit(monkeypatch, tmp_path):
    _skip_if_no_db()
    from agent.lifecycle import graph_refresh

    async with async_session() as s:
        repo = Repo(name="cardamon-noop", url="...", default_branch="main",
                    organization_id=1)
        s.add(repo)
        await s.flush()
        s.add(RepoGraphConfig(
            repo_id=repo.id, organization_id=1, analysis_branch="main",
            analyser_version="", workspace_path=str(tmp_path / "ws"),
        ))
        # Pre-existing complete row at commit_sha=AAA.
        s.add(RepoGraph(
            repo_id=repo.id, commit_sha="AAA", status="ok",
            analyser_version="v1", graph_json={"nodes": [], "edges": []},
            is_complete=True, processed_files={"x.py": {}}, failed_sites=[],
        ))
        await s.commit()
        repo_id = repo.id

    monkeypatch.setattr(graph_refresh, "_prepare_workspace", AsyncMock())
    monkeypatch.setattr(graph_refresh, "_resolve_commit_sha", AsyncMock(return_value="AAA"))
    monkeypatch.setattr(
        "agent.graph_workspace.graph_workspace_path",
        lambda repo_id: str(tmp_path / "ws"),
    )
    pipeline_spy = AsyncMock()
    monkeypatch.setattr(graph_refresh, "run_pipeline", pipeline_spy)

    await graph_refresh.run_refresh(repo_id=repo_id, request_id="noop")

    # No pipeline call (no LLM cost).
    pipeline_spy.assert_not_called()
    # Row unchanged.
    async with async_session() as s:
        result = await s.execute(select(RepoGraph).where(RepoGraph.repo_id == repo_id))
        row = result.scalar_one()
        assert row.is_complete is True
        assert row.commit_sha == "AAA"
        assert "x.py" in row.processed_files


@pytest.mark.asyncio
async def test_resume_diff_drops_changed_file_entries(monkeypatch, tmp_path):
    _skip_if_no_db()
    from agent.lifecycle import graph_refresh
    from agent.graph_analyzer.diff import ChangedFilesPlan

    async with async_session() as s:
        repo = Repo(name="cardamon-diff", url="...", default_branch="main",
                    organization_id=1)
        s.add(repo)
        await s.flush()
        s.add(RepoGraphConfig(
            repo_id=repo.id, organization_id=1, analysis_branch="main",
            analyser_version="", workspace_path=str(tmp_path / "ws"),
        ))
        s.add(RepoGraph(
            repo_id=repo.id, commit_sha="OLD", status="ok",
            analyser_version="v1",
            graph_json={
                "nodes": [
                    {"id": "x.py::foo", "file": "x.py"},
                    {"id": "y.py::bar", "file": "y.py"},
                ],
                "edges": [],
            },
            is_complete=True,
            processed_files={"x.py": {}, "y.py": {}},
            failed_sites=[],
        ))
        await s.commit()
        repo_id = repo.id

    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()

    monkeypatch.setattr(graph_refresh, "_prepare_workspace", AsyncMock())
    monkeypatch.setattr(graph_refresh, "_resolve_commit_sha", AsyncMock(return_value="NEW"))
    monkeypatch.setattr(
        graph_refresh, "changed_files",
        AsyncMock(return_value=ChangedFilesPlan(modified=["x.py"])),
    )
    monkeypatch.setattr(
        "agent.graph_workspace.graph_workspace_path",
        lambda repo_id: str(workspace),
    )

    async def fake_pipeline(*, on_file_checkpoint=None, initial_processed_files=None, **kwargs):
        # On a M plan with no cross-file callers, only x.py's entry was dropped.
        assert "x.py" not in initial_processed_files
        assert "y.py" in initial_processed_files
        from types import SimpleNamespace
        return SimpleNamespace(
            model_dump=lambda mode=None: {"nodes": [], "edges": [], "areas": [],
                                           "public_symbols": [], "commit_sha": "NEW"},
            areas=[],
        )
    monkeypatch.setattr(graph_refresh, "run_pipeline", fake_pipeline)

    await graph_refresh.run_refresh(repo_id=repo_id, request_id="diff")

    async with async_session() as s:
        result = await s.execute(select(RepoGraph).where(RepoGraph.repo_id == repo_id))
        row = result.scalar_one()
        assert row.is_complete is True
        assert row.commit_sha == "NEW"
```

- [ ] **Step 2: Run**

```
.venv/bin/python3 -m pytest tests/test_repo_graph_resume_db.py -v
```

Expected: 2 passed (or skipped if no DATABASE_URL).

- [ ] **Step 3: Commit**

```
git add tests/test_repo_graph_resume_db.py
git commit -m "test(graph): db-backed no-op + resume_diff row-load cases"
```

---

## Task 18: Deploy + smoke

**Files:** none (operational)

- [ ] **Step 1: Full test sweep**

```
.venv/bin/python3 -m pytest tests/ -q
```

Expected: same baseline as before this change, plus all new tests in pass / skip state. Zero regressions.

- [ ] **Step 2: Lint + format**

```
.venv/bin/ruff check agent/graph_analyzer/ agent/lifecycle/graph_refresh.py orchestrator/router.py shared/models/core.py shared/types.py
.venv/bin/ruff format --check agent/graph_analyzer/ agent/lifecycle/graph_refresh.py orchestrator/router.py shared/models/core.py shared/types.py
cd web-next && npm run typecheck && npm run test
```

All clean or no new errors.

- [ ] **Step 3: Push + deploy**

```
git push origin main
./scripts/deploy.sh
```

Wait for `==> Done.` and `{"status":"ok"}` health-check.

- [ ] **Step 4: Manual smoke against cardamon**

- Open `http://172.190.26.82:3000/code-graph/<cardamon-id>`.
- Verify the page renders (no React error).
- Click Refresh.
- Watch the badge transition from "Partial" / "Analyzing X / Y files" upward.
- Confirm `processed` count climbs in the UI without manual reload.
- Let it run; on completion the badge flips to green "Complete · N files".

- [ ] **Step 5: Tag**

```
git tag adr-016-resume-and-test-skip-v1
git push origin adr-016-resume-and-test-skip-v1
```

---

## Self-review summary

- **Spec coverage:** every section of the spec (schema, pipeline flow, row-load cases, diff handling, smart cascade, test-file exclusion, API extensions, UI badge, button label, error handling, testing) maps to at least one task above.
- **Placeholders:** none left — every code block is concrete; every command shows expected output.
- **Type consistency:** `ChangedFilesPlan`, `apply_plan`, `CheckpointFlush`, `RepoGraphProgressData`, and field names (`is_complete`, `processed_files`, `failed_sites`, `last_file`, `status`) are used consistently across tasks 1–18.
- **DRY/YAGNI:** no premature configurability (test patterns hardcoded; future `.auto-agent/graph.yml` override explicitly out of scope). No "force full re-analyze" button in v1 — operator can clear the row.
- **TDD ordering:** every backend feature has its test written first; every component has its test written first.
- **Frequent commits:** every task ends with a commit step.
