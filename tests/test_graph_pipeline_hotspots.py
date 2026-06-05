"""Integration tests: run_pipeline populates RepoGraphBlob.hotspots (ADR-016 Phase 12).

Test A — Git repo workspace:
    Build a tmp git repo with two areas:
      - complex_area/complex.py : a Python file with several branchy functions
        (high cyclomatic complexity), committed multiple times.
      - simple_area/simple.py  : a minimal file (low complexity), committed once.
    Run run_pipeline; assert blob.hotspots ranks complex.py at the top.

Test B — Non-git workspace:
    Copy a static fixture (no git) into tmp_path; run run_pipeline; assert
    blob.hotspots is empty.  (Also verifies existing pipeline tests are safe.)

Both tests skip if git is unavailable (only Test A needs git).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent.graph_analyzer.pipeline import run_pipeline
from shared.types import RepoGraphBlob

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURE = Path(__file__).parent / "fixtures" / "graph_repo_python"


def _git_available() -> bool:
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Python source for complex file (high cyclomatic complexity)
# ---------------------------------------------------------------------------

_COMPLEX_PY = '''\
"""A deliberately complex module for hotspot-ranking integration tests."""


def process_request(request, mode):
    """Many branches → high cyclomatic complexity."""
    if request is None:
        return None
    if mode == "fast":
        if request.get("urgent"):
            return _fast_path(request)
        elif request.get("priority"):
            return _priority_path(request)
        else:
            return _default_fast(request)
    elif mode == "slow":
        result = None
        for item in request.get("items", []):
            if item.get("active"):
                if item.get("type") == "a":
                    result = _handle_a(item)
                elif item.get("type") == "b":
                    result = _handle_b(item)
                else:
                    result = _handle_other(item)
        return result
    else:
        raise ValueError(f"Unknown mode: {mode}")


def validate_payload(payload, strict=False):
    """Another branchy function."""
    if not isinstance(payload, dict):
        return False
    if "id" not in payload:
        return False
    if strict:
        if "name" not in payload:
            return False
        if "type" not in payload:
            return False
        allowed = {"a", "b", "c"}
        if payload["type"] not in allowed:
            return False
    return True


def _fast_path(req):
    return {"status": "fast", "data": req}


def _priority_path(req):
    return {"status": "priority", "data": req}


def _default_fast(req):
    return {"status": "default_fast", "data": req}


def _handle_a(item):
    return item


def _handle_b(item):
    return item


def _handle_other(item):
    return item
'''

_SIMPLE_PY = '''\
"""A deliberately simple module for hotspot-ranking integration tests."""


def add(a, b):
    """Simple function — low complexity."""
    return a + b
'''

_COMPLEX_INIT = ""
_SIMPLE_INIT = ""


# ---------------------------------------------------------------------------
# Build the git repo fixture inline
# ---------------------------------------------------------------------------


def _build_git_repo(tmp_path: Path) -> str:
    """Create a git repo workspace with complex_area and simple_area.

    complex_area/complex.py is committed 3 times (high churn).
    simple_area/simple.py is committed once (low churn).

    Returns the workspace root as a string.
    """
    ws = tmp_path / "ws"
    ws.mkdir()

    def _run(*args: str) -> None:
        result = subprocess.run(
            list(args),
            cwd=str(ws),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git command {args!r} failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
            )

    _run("git", "init")
    _run("git", "config", "user.email", "test@example.com")
    _run("git", "config", "user.name", "Test User")

    # Create directory structure.
    (ws / "complex_area").mkdir()
    (ws / "simple_area").mkdir()
    (ws / "complex_area" / "__init__.py").write_text(_COMPLEX_INIT)
    (ws / "simple_area" / "__init__.py").write_text(_SIMPLE_INIT)

    # First commit: add both files.
    (ws / "complex_area" / "complex.py").write_text(_COMPLEX_PY)
    (ws / "simple_area" / "simple.py").write_text(_SIMPLE_PY)
    _run("git", "add", "complex_area/__init__.py")
    _run("git", "add", "simple_area/__init__.py")
    _run("git", "add", "complex_area/complex.py")
    _run("git", "add", "simple_area/simple.py")
    _run("git", "commit", "-m", "initial commit")

    # Second commit: modify complex.py only.
    (ws / "complex_area" / "complex.py").write_text(_COMPLEX_PY + "\n# rev 2\n")
    _run("git", "add", "complex_area/complex.py")
    _run("git", "commit", "-m", "second commit complex")

    # Third commit: modify complex.py again.
    (ws / "complex_area" / "complex.py").write_text(_COMPLEX_PY + "\n# rev 3\n")
    _run("git", "add", "complex_area/complex.py")
    _run("git", "commit", "-m", "third commit complex")

    return str(ws)


# ---------------------------------------------------------------------------
# Test A: git repo — complex+churned file ranks highest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not _git_available(), reason="git not available on PATH")
async def test_hotspots_complex_churned_file_ranks_highest(tmp_path: Path) -> None:
    """complex.py (3 commits, high cyclomatic) ranks above simple.py (1 commit, low complexity)."""
    ws = _build_git_repo(tmp_path)

    blob = await run_pipeline(workspace=ws, commit_sha="test_hotspot_sha")

    assert isinstance(blob, RepoGraphBlob)
    assert isinstance(blob.hotspots, list), "blob.hotspots must be a list"

    # There must be at least one hotspot (complex.py must appear).
    assert len(blob.hotspots) >= 1, (
        f"Expected at least 1 hotspot; got 0. "
        f"Nodes: {[(n.file, n.kind, n.cyclomatic) for n in blob.nodes if n.kind == 'function']}"
    )

    # complex.py must be at the top of the rankings.
    top = blob.hotspots[0]
    assert "complex.py" in top.file, (
        f"Expected complex.py at hotspot[0], got {top.file!r}. "
        f"Full hotspots: {[(h.file, h.score) for h in blob.hotspots]}"
    )

    # simple.py should either not appear (no cyclomatic on its single-line function)
    # or rank strictly below complex.py.
    simple_entries = [h for h in blob.hotspots if "simple.py" in h.file]
    if simple_entries:
        assert simple_entries[0].score <= top.score


# ---------------------------------------------------------------------------
# Test B: non-git workspace → hotspots is empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hotspots_empty_on_non_git_workspace(tmp_path: Path) -> None:
    """run_pipeline on a non-git tmp workspace produces blob.hotspots == []."""
    # Copy a static fixture (no git history) into tmp_path.
    target = tmp_path / "workspace"
    shutil.copytree(_FIXTURE, target)

    blob = await run_pipeline(workspace=str(target), commit_sha="no_git_sha")

    assert isinstance(blob, RepoGraphBlob)
    assert blob.hotspots == [], (
        f"Expected empty hotspots on non-git workspace, got: {blob.hotspots}"
    )
