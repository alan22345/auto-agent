"""Regression test for bug 18 — the ``coder produces no diff`` cascade.

``agent.workspace.create_branch`` defensively stashes untracked / uncommitted
state before switching branches so a leftover ``.gitignore`` (or any other
untracked file that would clash with the destination branch's tracked
copy) doesn't trip ``git checkout``. The stash is intentionally NOT popped
— popping it would just re-introduce the conflict on the next switch.

The original implementation called ``git stash push --include-untracked``
unconditionally, which swept the *entire* working tree's untracked content
into the stash — including the architect's freshly-written
``.auto-agent/design.md``. The next phase (the coder subagent) then ran
against an empty workspace, produced no diff, and the trio bailed out
with ``coder_produced_no_diff``.

Fix: ``clone_repo`` now writes ``.auto-agent/``, ``.venv/`` and
``__pycache__/`` into ``.git/info/exclude`` immediately after the clone
(or on the workspace-reuse path). ``--include-untracked`` skips ignored
files, so the artefacts survive the branch switch and the coder can read
``design.md`` as the architect intended.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from agent import workspace


def _git(*args: str, cwd: str) -> str:
    """Run git with deterministic identity + no global config bleed."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout


def _init_repo_with_branch(tmp_path: Path) -> str:
    """Create a tiny git repo with ``main`` and an integration branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=str(repo))
    (repo / "README.md").write_text("hi\n")
    _git("add", "README.md", cwd=str(repo))
    _git("commit", "-m", "init", cwd=str(repo))
    # Branch the architect would have left for the coder to commit onto.
    _git("checkout", "-b", "trio/42", cwd=str(repo))
    _git("checkout", "main", cwd=str(repo))
    return str(repo)


def test_ensure_local_excludes_appends_patterns(tmp_path: Path) -> None:
    """``_ensure_local_excludes`` writes the three patterns and is idempotent."""

    repo = _init_repo_with_branch(tmp_path)

    workspace._ensure_local_excludes(repo)
    exclude_path = Path(repo) / ".git" / "info" / "exclude"
    body = exclude_path.read_text()
    assert ".auto-agent/" in body
    assert ".venv/" in body
    assert "__pycache__/" in body

    # Idempotent: second call must not duplicate any line.
    workspace._ensure_local_excludes(repo)
    body2 = exclude_path.read_text()
    assert body2.count(".auto-agent/") == 1
    assert body2.count(".venv/") == 1
    assert body2.count("__pycache__/") == 1


def test_create_branch_preserves_auto_agent_artifacts(tmp_path: Path) -> None:
    """The architect's ``.auto-agent/design.md`` must survive a branch switch.

    Reproduces bug 18: before the fix the design.md would be stashed away by
    ``create_branch`` and the coder would see an empty ``.auto-agent/`` dir.
    """

    repo = _init_repo_with_branch(tmp_path)
    workspace._ensure_local_excludes(repo)

    # Simulate the architect's output sitting in the workspace BEFORE we
    # switch to the integration branch.
    auto_agent = Path(repo) / ".auto-agent"
    auto_agent.mkdir()
    design = auto_agent / "design.md"
    design.write_text("# Design — task 42\nbuild the thing.\n")

    asyncio.run(workspace.create_branch(repo, "trio/42"))

    # After the switch, design.md must still be on disk for the coder to read.
    assert design.exists(), (
        "create_branch must NOT stash .auto-agent/design.md when switching "
        "branches — the coder needs it on disk."
    )
    assert design.read_text().startswith("# Design — task 42")

    # Sanity: the branch switch did happen.
    current = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).strip()
    assert current == "trio/42"


def test_create_branch_still_handles_conflicting_gitignore(tmp_path: Path) -> None:
    """Bug 13's stash protection must still work: an untracked ``.gitignore``
    that would conflict with a tracked one on the destination branch is
    handled (stashed) so the checkout succeeds.

    This is the original reason ``create_branch`` was given the stash
    fallback — fixing bug 18 must not regress bug 13.
    """

    repo = _init_repo_with_branch(tmp_path)
    workspace._ensure_local_excludes(repo)

    # Add a tracked .gitignore on the destination branch.
    _git("checkout", "trio/42", cwd=repo)
    (Path(repo) / ".gitignore").write_text("node_modules/\n")
    _git("add", ".gitignore", cwd=repo)
    _git("commit", "-m", "add gitignore", cwd=repo)
    _git("checkout", "main", cwd=repo)

    # Now drop an untracked .gitignore that would conflict.
    (Path(repo) / ".gitignore").write_text("dist/\n")

    # The switch must succeed despite the conflict.
    asyncio.run(workspace.create_branch(repo, "trio/42"))
    current = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).strip()
    assert current == "trio/42"


@pytest.mark.asyncio
async def test_clone_repo_seeds_excludes_on_reuse_path(tmp_path: Path, monkeypatch) -> None:
    """``clone_repo`` must seed ``.git/info/exclude`` BEFORE the reuse-path
    fetch/checkout/reset triplet — otherwise a stale ``.auto-agent/`` left
    by the prior phase would survive only by luck.
    """

    repo = _init_repo_with_branch(tmp_path)
    workspaces_dir = tmp_path / "workspaces"
    workspaces_dir.mkdir()
    # Mirror the workspace under the location clone_repo will resolve to.
    target = workspaces_dir / "1" / "task-42"
    target.parent.mkdir(parents=True)
    subprocess.run(["cp", "-R", repo, str(target)], check=True)

    # Pretend there's no auth token + no DB call needed.
    async def _no_token(**_kw):
        return None

    monkeypatch.setattr("shared.github_auth.get_github_token", _no_token)
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", str(workspaces_dir))

    await workspace.clone_repo(
        repo_url=f"file://{repo}",
        task_id=42,
        default_branch="main",
        organization_id=1,
    )

    exclude_path = target / ".git" / "info" / "exclude"
    assert exclude_path.exists()
    body = exclude_path.read_text()
    assert ".auto-agent/" in body
