"""push_branch must integrate an existing remote branch instead of crashing.

Incident (task #327): re-running a task whose branch already carries an open PR
re-clones from base, commits on top of base, then ``push_branch`` did a bare
``git push`` — rejected non-fast-forward because origin's branch is ahead. The
task died with a "(non-fast-forward)" error in Slack even though the work was
already shipped. Fix: fetch + rebase onto ``origin/<branch>`` (never force,
never clobber the PR) and retry the push.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from agent.workspace import push_branch

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@e.com",
        }
    )
    return subprocess.run(
        ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, check=True
    ).stdout


def _clone(root: Path, origin: Path, name: str) -> Path:
    """Clone origin into root/name and pin a local identity (as clone_repo does)."""
    dest = root / name
    _git(root, "clone", "-q", str(origin), str(dest))
    _git(dest, "config", "user.email", "auto-agent@bot.local")
    _git(dest, "config", "user.name", "auto-agent")
    return dest


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A bare origin seeded with a `main` containing one commit."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    seed = _clone(tmp_path, origin, "seed")
    (seed / "README.md").write_text("base\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "base")
    _git(seed, "push", "-q", "origin", "main")
    return origin


def _seed_pr_branch(root: Path, origin: Path) -> None:
    """Simulate a prior run that pushed `feat` with the shipped PR work."""
    first = _clone(root, origin, "first")
    _git(first, "checkout", "-q", "-b", "feat")
    (first / "pr_work.txt").write_text("the shipped PR work\n")
    _git(first, "add", ".")
    _git(first, "commit", "-q", "-m", "PR #12 work")
    _git(first, "push", "-q", "-u", "origin", "feat")


@pytest.mark.asyncio
async def test_push_branch_integrates_existing_remote_branch(remote, tmp_path):
    _seed_pr_branch(tmp_path, remote)

    # Re-run: fresh clone from main, new `feat` from base, a redundant extra fix.
    second = _clone(tmp_path, remote, "second")
    _git(second, "checkout", "-q", "-b", "feat")  # from main, NOT origin/feat
    (second / "rerun_fix.txt").write_text("a small extra fix\n")
    _git(second, "add", ".")
    _git(second, "commit", "-q", "-m", "rerun fix")

    # A bare push here is rejected non-fast-forward; push_branch must integrate
    # origin/feat and succeed without losing the PR work.
    await push_branch(str(second), "feat")

    verify = _clone(tmp_path, remote, "verify")
    _git(verify, "checkout", "-q", "feat")
    assert (verify / "pr_work.txt").exists(), "PR #12 work must survive (no clobber)"
    assert (verify / "rerun_fix.txt").exists(), "the re-run's work must land on top"


@pytest.mark.asyncio
async def test_push_branch_noop_when_remote_already_has_work(remote, tmp_path):
    _seed_pr_branch(tmp_path, remote)

    # Re-run that produces NO new commit — local `feat` is just base, behind
    # origin/feat. push_branch must fast-forward + push without raising.
    second = _clone(tmp_path, remote, "second")
    _git(second, "checkout", "-q", "-b", "feat")  # from main, no new commits

    await push_branch(str(second), "feat")  # must not raise

    verify = _clone(tmp_path, remote, "verify")
    _git(verify, "checkout", "-q", "feat")
    assert (verify / "pr_work.txt").exists()
