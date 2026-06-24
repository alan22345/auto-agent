# Auto-Heal Loop — Phase 3: CleanupBranchManager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Own the long-lived cleanup branch — create it, keep it rebased onto the base branch, merge accepted fixes into it — and force-push it *safely* via an allowlist guard so a bug can never force-push `main` or a user branch.

**Architecture:** Orchestration-layer git, mirroring `agent/workspace.py` (`agent.sh.run`-based git, NOT the agent's sandboxed `agent/tools/git.py` — that only constrains the in-agent tool). The deliberate guardrail-loosening lives *inside* this module: `_force_push_cleanup` refuses any branch not in an explicit allowlist. Real-git integration tests (temp bare origin + working clone) because git behavior — rebase conflicts, merge aborts — can't be faithfully mocked.

**Tech Stack:** Python 3.12, async, `agent.sh.run`, dataclasses, pytest + `subprocess` (test fixtures only) + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-06-09-auto-heal-loop-design.md` (component 6). **Correction to spec:** the carve-out is NOT in `agent/tools/git.py`; it's the in-module allowlist guard described here.

---

## File structure

- **Create:** `agent/health_loop/cleanup_branch.py` — `DEFAULT_CLEANUP_BRANCH`, `RebaseOutcome`, `_git`, `_force_push_cleanup`, `ensure_cleanup_branch`, `rebase_onto_base`, `merge_fix`. One responsibility: the cleanup-branch lifecycle.
- **Create:** `tests/test_health_loop_cleanup_branch.py` — a `git_repos` fixture (temp bare origin + working clone) and integration tests + the guard unit test.

### Reference: how orchestration runs git (from `agent/workspace.py`)

```python
from agent import sh
result = await sh.run(["git", *args], cwd=cwd, timeout=60)
# result.failed: bool, result.stdout: str, result.stderr: str, result.returncode: int | None
```

---

### Task 1: `_git` helper + `_force_push_cleanup` allowlist guard

**Files:**
- Create: `agent/health_loop/cleanup_branch.py`
- Test: `tests/test_health_loop_cleanup_branch.py`

- [ ] **Step 1: Write the failing test (guard refusal — pure, no git needed)**

Create `tests/test_health_loop_cleanup_branch.py`:

```python
"""Phase 3 — cleanup-branch lifecycle (real-git integration tests)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.health_loop import cleanup_branch
from agent.health_loop.cleanup_branch import (
    DEFAULT_CLEANUP_BRANCH,
    RebaseOutcome,
    ensure_cleanup_branch,
    merge_fix,
    rebase_onto_base,
)


@pytest.mark.asyncio
async def test_force_push_refuses_branch_not_in_allowlist():
    """The guardrail: force-pushing anything outside the allowlist raises
    BEFORE any git command runs."""
    from unittest.mock import AsyncMock, patch

    sh_run = AsyncMock()
    with patch.object(cleanup_branch.sh, "run", sh_run):
        with pytest.raises(ValueError, match="allowlist"):
            await cleanup_branch._force_push_cleanup(
                workspace="/tmp/x",
                cleanup_branch="main",  # NOT the cleanup branch
                allowed_branches={DEFAULT_CLEANUP_BRANCH},
            )
    sh_run.assert_not_called()  # never reached the push
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_cleanup_branch.py -k force_push_refuses -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent.health_loop.cleanup_branch'`

- [ ] **Step 3: Implement `_git`, the guard, and the result type**

Create `agent/health_loop/cleanup_branch.py`:

```python
"""Cleanup-branch lifecycle for the auto-heal loop.

Owns the long-lived branch that stages accepted health fixes. Operations:
create-if-missing, rebase-onto-base (keeping the branch current with
``main``), and merge-accepted-fix. The branch is never auto-merged to the
base — a human reviews and merges it.

This is orchestration-layer git (``agent.sh.run``), like
:mod:`agent.workspace` — NOT the sandboxed in-agent ``agent/tools/git.py``.
The one deliberately-loosened guardrail (force-push) is constrained here by
:func:`_force_push_cleanup`, which refuses any branch outside an explicit
allowlist so a bug can never force-push ``main`` or a user branch.
"""
from __future__ import annotations

from dataclasses import dataclass

from agent import sh

DEFAULT_CLEANUP_BRANCH = "auto-agent/health-cleanup"


@dataclass
class RebaseOutcome:
    """Result of :func:`rebase_onto_base`.

    ``ok`` True ⇒ rebased clean and force-pushed. ``conflict`` True ⇒ the
    rebase hit a conflict, was aborted, and the branch is unchanged (the
    caller parks it for human resolution).
    """

    ok: bool
    conflict: bool = False
    detail: str = ""


async def _git(*args: str, cwd: str, check: bool = True):
    """Run a git command via the shared subprocess seam.

    Returns the ``sh`` result (``.failed``/``.stdout``/``.stderr``). Raises
    ``RuntimeError`` on failure when ``check`` is True.
    """
    result = await sh.run(["git", *args], cwd=cwd, timeout=60)
    if check and result.failed:
        raise RuntimeError(
            f"git {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    return result


async def _force_push_cleanup(
    *, workspace: str, cleanup_branch: str, allowed_branches: set[str]
) -> None:
    """Force-push ``cleanup_branch`` to origin — ONLY if allowlisted.

    The single deliberately-loosened guardrail in the auto-heal loop. The
    allowlist check runs before any git command so a misconfigured caller
    can never force-push a non-cleanup branch.
    """
    if cleanup_branch not in allowed_branches:
        raise ValueError(
            f"refusing to force-push {cleanup_branch!r}: not in the cleanup "
            f"allowlist {sorted(allowed_branches)}"
        )
    await _git("push", "--force-with-lease", "origin", cleanup_branch, cwd=workspace)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_cleanup_branch.py -k force_push_refuses -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/health_loop/cleanup_branch.py tests/test_health_loop_cleanup_branch.py
git commit -m "feat(health-loop): cleanup-branch force-push allowlist guard"
```

---

### Task 2: `git_repos` fixture + `ensure_cleanup_branch`

**Files:**
- Modify: `agent/health_loop/cleanup_branch.py`
- Test: `tests/test_health_loop_cleanup_branch.py`

- [ ] **Step 1: Add the fixture + failing test**

Append to `tests/test_health_loop_cleanup_branch.py`:

```python
def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def _set_identity(work: Path):
    _run(["git", "config", "user.email", "test@auto-agent.local"], work)
    _run(["git", "config", "user.name", "auto-agent-test"], work)


@pytest.fixture
def git_repos(tmp_path):
    """A bare 'origin' + a working clone with a single commit on 'main'."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(work)], check=True, capture_output=True)
    _set_identity(work)
    (work / "README.md").write_text("base\n")
    _run(["git", "add", "."], work)
    _run(["git", "commit", "-m", "init"], work)
    _run(["git", "push", "-u", "origin", "main"], work)
    return SimpleNamespace(origin=str(origin), work=str(work))


def _current_branch(work: str) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=work, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _remote_has_branch(origin: str, branch: str) -> bool:
    out = subprocess.run(
        ["git", "ls-remote", "--heads", origin, branch], check=True, capture_output=True, text=True
    )
    return bool(out.stdout.strip())


@pytest.mark.asyncio
async def test_ensure_creates_cleanup_branch_off_base_when_missing(git_repos):
    await ensure_cleanup_branch(
        workspace=git_repos.work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH
    )
    # Now checked out on the cleanup branch, and it exists on origin.
    assert _current_branch(git_repos.work) == DEFAULT_CLEANUP_BRANCH
    assert _remote_has_branch(git_repos.origin, DEFAULT_CLEANUP_BRANCH)


@pytest.mark.asyncio
async def test_ensure_is_idempotent_when_branch_already_exists(git_repos):
    await ensure_cleanup_branch(
        workspace=git_repos.work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH
    )
    # Second call must not fail and must leave us on the cleanup branch.
    await ensure_cleanup_branch(
        workspace=git_repos.work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH
    )
    assert _current_branch(git_repos.work) == DEFAULT_CLEANUP_BRANCH
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_cleanup_branch.py -k ensure -q`
Expected: FAIL — `ImportError: cannot import name 'ensure_cleanup_branch'` (it's imported at top but undefined)

- [ ] **Step 3: Implement `ensure_cleanup_branch`**

Add to `agent/health_loop/cleanup_branch.py`:

```python
async def ensure_cleanup_branch(
    *, workspace: str, base_branch: str, cleanup_branch: str
) -> None:
    """Check out the cleanup branch, creating it off ``base_branch`` if it
    doesn't exist yet on the remote. Idempotent.
    """
    await _git("fetch", "origin", cwd=workspace)
    existing = await _git(
        "ls-remote", "--heads", "origin", cleanup_branch, cwd=workspace, check=False
    )
    if existing.stdout.strip():
        await _git("checkout", "-B", cleanup_branch, f"origin/{cleanup_branch}", cwd=workspace)
    else:
        await _git("checkout", "-B", cleanup_branch, f"origin/{base_branch}", cwd=workspace)
        await _git("push", "-u", "origin", cleanup_branch, cwd=workspace)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_cleanup_branch.py -k ensure -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add agent/health_loop/cleanup_branch.py tests/test_health_loop_cleanup_branch.py
git commit -m "feat(health-loop): ensure_cleanup_branch creates/checks out branch"
```

---

### Task 3: `merge_fix` — bring an accepted fix onto the cleanup branch

**Files:**
- Modify: `agent/health_loop/cleanup_branch.py`
- Test: `tests/test_health_loop_cleanup_branch.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health_loop_cleanup_branch.py`:

```python
def _commit_file(work: str, name: str, content: str, msg: str, branch: str | None = None):
    if branch is not None:
        _run(["git", "checkout", "-b", branch], work)
    (Path(work) / name).write_text(content)
    _run(["git", "add", "."], work)
    _run(["git", "commit", "-m", msg], work)


@pytest.mark.asyncio
async def test_merge_fix_brings_fix_commit_onto_cleanup(git_repos):
    work = git_repos.work
    await ensure_cleanup_branch(workspace=work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH)
    # A fix branch off the cleanup tip adds a file.
    _commit_file(work, "fix.py", "print('fixed')\n", "health fix", branch="fix/dead-code")

    merged = await merge_fix(workspace=work, fix_branch="fix/dead-code", cleanup_branch=DEFAULT_CLEANUP_BRANCH)

    assert merged is True
    # The fix file is present on the cleanup branch and pushed to origin.
    _run(["git", "checkout", DEFAULT_CLEANUP_BRANCH], work)
    assert (Path(work) / "fix.py").exists()
    log = subprocess.run(
        ["git", "log", "--oneline", "origin/" + DEFAULT_CLEANUP_BRANCH],
        cwd=work, check=True, capture_output=True, text=True,
    ).stdout
    assert "health fix" in log
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_cleanup_branch.py -k merge_fix -q`
Expected: FAIL — `ImportError: cannot import name 'merge_fix'`

- [ ] **Step 3: Implement `merge_fix`**

Add to `agent/health_loop/cleanup_branch.py`:

```python
async def merge_fix(
    *, workspace: str, fix_branch: str, cleanup_branch: str
) -> bool:
    """Merge an accepted fix branch into the cleanup branch and push.

    Returns True on a clean merge (pushed), False if the merge conflicted
    (aborted, cleanup branch unchanged). The push is an ordinary
    fast-forward of the cleanup branch — no force needed.
    """
    await _git("fetch", "origin", cwd=workspace)
    await _git("checkout", "-B", cleanup_branch, f"origin/{cleanup_branch}", cwd=workspace)
    merged = await _git(
        "merge", "--no-ff", "-m", f"health: merge {fix_branch}", fix_branch,
        cwd=workspace, check=False,
    )
    if merged.failed:
        await _git("merge", "--abort", cwd=workspace, check=False)
        return False
    await _git("push", "origin", cleanup_branch, cwd=workspace)
    return True
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_cleanup_branch.py -k merge_fix -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/health_loop/cleanup_branch.py tests/test_health_loop_cleanup_branch.py
git commit -m "feat(health-loop): merge_fix lands accepted fix on cleanup branch"
```

---

### Task 4: `rebase_onto_base` — keep cleanup current; conflict → abort

**Files:**
- Modify: `agent/health_loop/cleanup_branch.py`
- Test: `tests/test_health_loop_cleanup_branch.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health_loop_cleanup_branch.py`:

```python
def _advance_main(git_repos, name: str, content: str, msg: str):
    """Add a commit to main on origin (via a throwaway clone)."""
    work = git_repos.work
    _run(["git", "checkout", "main"], work)
    (Path(work) / name).write_text(content)
    _run(["git", "add", "."], work)
    _run(["git", "commit", "-m", msg], work)
    _run(["git", "push", "origin", "main"], work)


@pytest.mark.asyncio
async def test_rebase_onto_base_replays_cleanup_on_advanced_main(git_repos):
    work = git_repos.work
    await ensure_cleanup_branch(workspace=work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH)
    # After ensure_cleanup_branch we are on the cleanup branch. Put a
    # non-conflicting fix on it (new file) and push.
    (Path(work) / "fix.py").write_text("print('fixed')\n")
    _run(["git", "add", "."], work)
    _run(["git", "commit", "-m", "health fix"], work)
    _run(["git", "push", "origin", DEFAULT_CLEANUP_BRANCH], work)
    # main advances with a DIFFERENT file (no conflict).
    _advance_main(git_repos, "main.py", "print('main')\n", "advance main")

    outcome = await rebase_onto_base(
        workspace=work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH,
        allowed_branches={DEFAULT_CLEANUP_BRANCH},
    )

    assert isinstance(outcome, RebaseOutcome)
    assert outcome.ok is True and outcome.conflict is False
    # Cleanup now contains BOTH main's new file and the fix, on origin.
    _run(["git", "fetch", "origin"], work)
    files = subprocess.run(
        ["git", "ls-tree", "--name-only", "origin/" + DEFAULT_CLEANUP_BRANCH],
        cwd=work, check=True, capture_output=True, text=True,
    ).stdout
    assert "fix.py" in files and "main.py" in files


@pytest.mark.asyncio
async def test_rebase_conflict_aborts_and_reports(git_repos):
    work = git_repos.work
    await ensure_cleanup_branch(workspace=work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH)
    # Cleanup edits README.
    (Path(work) / "README.md").write_text("cleanup change\n")
    _run(["git", "add", "."], work)
    _run(["git", "commit", "-m", "cleanup edits README"], work)
    _run(["git", "push", "origin", DEFAULT_CLEANUP_BRANCH], work)
    # main edits the SAME line ⇒ rebase conflict.
    _advance_main(git_repos, "README.md", "main change\n", "main edits README")

    outcome = await rebase_onto_base(
        workspace=work, base_branch="main", cleanup_branch=DEFAULT_CLEANUP_BRANCH,
        allowed_branches={DEFAULT_CLEANUP_BRANCH},
    )

    assert outcome.ok is False and outcome.conflict is True
    # The repo is not mid-rebase (abort cleaned up).
    status = subprocess.run(["git", "status"], cwd=work, capture_output=True, text=True).stdout
    assert "rebase in progress" not in status.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_cleanup_branch.py -k rebase -q`
Expected: FAIL — `ImportError: cannot import name 'rebase_onto_base'`

- [ ] **Step 3: Implement `rebase_onto_base`**

Add to `agent/health_loop/cleanup_branch.py`:

```python
async def rebase_onto_base(
    *,
    workspace: str,
    base_branch: str,
    cleanup_branch: str,
    allowed_branches: set[str] | None = None,
) -> RebaseOutcome:
    """Rebase the cleanup branch onto the latest base, then force-push it.

    On a clean rebase the branch is force-pushed (via the allowlist guard)
    and ``ok=True``. On conflict the rebase is aborted, the branch is left
    unchanged, and ``conflict=True`` is returned for the caller to park.
    """
    allowed = allowed_branches or {cleanup_branch}
    await _git("fetch", "origin", cwd=workspace)
    await _git("checkout", "-B", cleanup_branch, f"origin/{cleanup_branch}", cwd=workspace)
    rebased = await _git("rebase", f"origin/{base_branch}", cwd=workspace, check=False)
    if rebased.failed:
        await _git("rebase", "--abort", cwd=workspace, check=False)
        return RebaseOutcome(
            ok=False,
            conflict=True,
            detail=(rebased.stderr.strip() or rebased.stdout.strip())[:300] or "rebase conflict",
        )
    await _force_push_cleanup(
        workspace=workspace, cleanup_branch=cleanup_branch, allowed_branches=allowed
    )
    return RebaseOutcome(ok=True)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_cleanup_branch.py -k rebase -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the full Phase 3 file + lint + format**

Run: `.venv/bin/python3 -m pytest tests/test_health_loop_cleanup_branch.py -q`
Expected: PASS (all)
Run: `.venv/bin/ruff check agent/health_loop/ tests/test_health_loop_cleanup_branch.py`
Expected: `All checks passed!`
Run: `.venv/bin/ruff format --check agent/health_loop/cleanup_branch.py tests/test_health_loop_cleanup_branch.py`
Expected: `2 files already formatted`

- [ ] **Step 6: Commit**

```bash
git add agent/health_loop/cleanup_branch.py tests/test_health_loop_cleanup_branch.py
git commit -m "feat(health-loop): rebase_onto_base keeps cleanup current, aborts on conflict"
```

---

### Phase 3 exit criteria

- `agent/health_loop/cleanup_branch.py` exposes `DEFAULT_CLEANUP_BRANCH`,
  `RebaseOutcome`, `ensure_cleanup_branch`, `merge_fix`, `rebase_onto_base`, and
  the `_force_push_cleanup` guard.
- Force-push is reachable ONLY through the allowlist guard; a non-allowlisted
  branch raises before any git runs (unit-tested).
- Rebase conflict aborts cleanly and reports (no dangling rebase state).
- All integration tests run against real temp git repos; ruff + format clean.

### Implementation notes

- These tests shell out to real `git` via `subprocess` for SETUP only; the code
  under test uses `agent.sh.run`. If `agent.sh.run` needs any env the tests don't
  provide, mirror what `agent/workspace.py` relies on (it sets nothing special —
  `GIT_TERMINAL_PROMPT=0` is inside the seam).
- Commit only the two Phase-3 files; do NOT `git add -A` (untracked
  `.claude/worktrees/` must stay out).
```
