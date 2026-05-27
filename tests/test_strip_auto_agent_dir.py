"""Integration-PR cleanup: strip ``.auto-agent/`` from the integration
branch before pushing.

The design phase commits ``.auto-agent/design.md`` mid-flow so a workspace
reset between phases doesn't lose it (see
``agent/lifecycle/trio/design_approval.py::_commit_design_md``). By the
time the integration PR is opened the trio is done; that commit no
longer needs to be in the branch. If we DON'T strip it, the PR merge
puts ``.auto-agent/design.md`` (with this task's id in its header) onto
``main``. The next task that clones ``main`` then inherits the stale
artefact and the architect's pinned context reads it as the contract
for the new task (task 29, 2026-05-27 — incident).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@auto-agent", cwd=repo)
    _git("config", "user.name", "auto-agent test", cwd=repo)
    return repo


@pytest.mark.asyncio
async def test_strip_removes_tracked_auto_agent_files(tmp_path: Path):
    from agent.lifecycle.trio import _strip_auto_agent_dir

    repo = _make_repo(tmp_path)
    (repo / "README.md").write_text("# project\n")
    (repo / ".auto-agent").mkdir()
    (repo / ".auto-agent" / "design.md").write_text(
        "<!-- auto-agent: task_id=42 -->\n\n# Design from a prior phase\n"
    )
    _git("add", "README.md", ".auto-agent/design.md", cwd=repo)
    _git("commit", "-q", "-m", "feat: initial + mid-flow design commit", cwd=repo)

    await _strip_auto_agent_dir(str(repo), parent_id=42)

    # ls-files should no longer list anything under .auto-agent/.
    ls = subprocess.run(
        ["git", "ls-files", ".auto-agent"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert ls.stdout.strip() == ""

    # The cleanup commit exists with the expected subject.
    log = subprocess.run(
        ["git", "log", "--format=%s", "-1"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "strip .auto-agent" in log.stdout


@pytest.mark.asyncio
async def test_strip_is_noop_when_auto_agent_not_tracked(tmp_path: Path):
    """When the integration branch never committed .auto-agent/, the
    strip step must do nothing — not error, not produce an empty
    commit, not pollute the log."""
    from agent.lifecycle.trio import _strip_auto_agent_dir

    repo = _make_repo(tmp_path)
    (repo / "README.md").write_text("# project\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "feat: initial", cwd=repo)

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    await _strip_auto_agent_dir(str(repo), parent_id=42)

    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_before == head_after, "no commit should be added when nothing to strip"


@pytest.mark.asyncio
async def test_strip_preserves_other_committed_files(tmp_path: Path):
    """Only .auto-agent/ goes; everything else stays."""
    from agent.lifecycle.trio import _strip_auto_agent_dir

    repo = _make_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('hello')\n")
    (repo / ".auto-agent").mkdir()
    (repo / ".auto-agent" / "design.md").write_text("stale\n")
    (repo / ".auto-agent" / "backlog.json").write_text("[]\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "feat: app + trio artefacts", cwd=repo)

    await _strip_auto_agent_dir(str(repo), parent_id=42)

    ls = subprocess.run(
        ["git", "ls-files"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "src/main.py" in ls
    assert not any(p.startswith(".auto-agent/") for p in ls), ls
