"""Tests for the Co-Authored-By commit-msg hook installed by clone_repo.

End-to-end: writes the hook into a real on-disk git repo (no clone required —
``git init`` is enough), runs a real ``git commit``, and asserts the trailer
landed in the resulting commit's message.

The DB lookup in ``install_coauthor_hook`` is bypassed by writing the trailer
file directly — we only verify the hook's shell-script behaviour here.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent.workspace import _COAUTHOR_HOOK_SCRIPT


def _git(repo: Path, *args: str, input: str | None = None) -> str:
    """Run a git command and return stdout. Raises on non-zero exit."""
    env = os.environ.copy()
    # Pin identity so tests don't depend on the dev's gitconfig.
    env.update({
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    })
    result = subprocess.run(
        ["git", *args], cwd=str(repo), env=env, input=input,
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def _install_hook(repo: Path, trailer: str) -> None:
    git_dir = repo / ".git"
    (git_dir / "auto-agent-coauthor").write_text(trailer + "\n")
    hooks = git_dir / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "commit-msg"
    hook.write_text(_COAUTHOR_HOOK_SCRIPT)
    hook.chmod(0o755)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Initialise a fresh git repo and return its root."""
    _git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


def test_hook_appends_trailer_to_simple_message(repo: Path):
    trailer = "Co-Authored-By: Alice Smith <alice@auto-agent.local>"
    _install_hook(repo, trailer)
    (repo / "x.txt").write_text("hello")
    _git(repo, "add", "x.txt")
    _git(repo, "commit", "-m", "Add hello file")
    msg = _git(repo, "log", "-1", "--format=%B").strip()
    assert "Add hello file" in msg
    assert trailer in msg
    # Trailer is at the very end with a blank line before it.
    assert msg.endswith(trailer)


def test_hook_is_idempotent_when_trailer_already_present(repo: Path):
    trailer = "Co-Authored-By: Alice Smith <alice@auto-agent.local>"
    _install_hook(repo, trailer)
    (repo / "y.txt").write_text("hi")
    _git(repo, "add", "y.txt")
    # User wrote a message that already contains the trailer.
    full_message = f"Add y\n\n{trailer}"
    _git(repo, "commit", "-m", full_message)
    msg = _git(repo, "log", "-1", "--format=%B").strip()
    # Should appear exactly once.
    assert msg.count(trailer) == 1


def test_hook_no_op_when_trailer_file_missing(repo: Path):
    _install_hook(repo, "Co-Authored-By: x <x@x>")
    (repo / ".git" / "auto-agent-coauthor").unlink()
    (repo / "z.txt").write_text("z")
    _git(repo, "add", "z.txt")
    _git(repo, "commit", "-m", "Add z")
    msg = _git(repo, "log", "-1", "--format=%B").strip()
    assert "Co-Authored-By" not in msg
