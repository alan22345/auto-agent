"""Tests for the pre-push commit safety net.

Root cause from task 48: the agent coded correctly and tests passed, but it
never ran `git commit`. _finish_coding blindly pushed the branch (which was
still at base's HEAD), then `gh pr create` failed with "No commits between
base and branch". Task failed silently and the workspace was cleaned up,
destroying the agent's work.

The safety net ensures that before push:
  1. Any uncommitted changes are auto-committed with a descriptive message
  2. If the branch has no commits relative to base, we raise an explicit
     error naming the problem — instead of producing a misleading PR failure
"""

import os
import subprocess
import tempfile

import pytest

from agent.workspace import (
    EmptyBranchError,
    commit_pending_changes,
    ensure_branch_has_commits,
)


@pytest.fixture
def git_repo():
    """Create a fresh git repo with a base commit and a feature branch."""
    tmp = tempfile.mkdtemp(prefix="wcs-test-")

    def run(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=tmp, capture_output=True, text=True, check=check,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

    run("init", "-b", "main")
    (open(os.path.join(tmp, "README.md"), "w")).write("hello\n")
    run("add", "-A")
    run("commit", "-m", "initial")
    run("checkout", "-b", "feature/test")

    yield tmp

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def _write(path: str, name: str, content: str):
    with open(os.path.join(path, name), "w") as f:
        f.write(content)


def _run(cwd: str, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


class TestCommitPendingChanges:
    @pytest.mark.asyncio
    async def test_commits_uncommitted_files(self, git_repo):
        """Reproduces task 48: agent wrote files but didn't commit."""
        _write(git_repo, "app.py", "print('hello')\n")
        _write(git_repo, "models.py", "class Foo: pass\n")

        # Verify there ARE uncommitted changes before
        status = _run(git_repo, "status", "--porcelain").stdout
        assert "app.py" in status and "models.py" in status

        committed = await commit_pending_changes(
            git_repo, task_id=48, title="Fix agent cloning bug",
        )
        assert committed is True

        # After: no uncommitted changes
        status = _run(git_repo, "status", "--porcelain").stdout
        assert status.strip() == ""

        # Commit message references task
        last_msg = _run(git_repo, "log", "-1", "--format=%s").stdout.strip()
        assert "#48" in last_msg
        assert "Fix agent cloning bug" in last_msg

    @pytest.mark.asyncio
    async def test_noop_when_nothing_to_commit(self, git_repo):
        """If the agent already committed, commit_pending_changes is a no-op."""
        _write(git_repo, "app.py", "x\n")
        _run(git_repo, "add", "-A")
        _run(git_repo, "commit", "-m", "agent committed")

        before_sha = _run(git_repo, "rev-parse", "HEAD").stdout.strip()
        committed = await commit_pending_changes(git_repo, 48, "anything")
        after_sha = _run(git_repo, "rev-parse", "HEAD").stdout.strip()

        assert committed is False
        assert before_sha == after_sha, "Should not create empty commits"

    @pytest.mark.asyncio
    async def test_commits_untracked_files_too(self, git_repo):
        """New files the agent created (never git-add'd) must be captured."""
        _write(git_repo, "brand_new.py", "new\n")
        # Note: never `git add` — file is untracked
        committed = await commit_pending_changes(git_repo, 48, "add new file")
        assert committed is True
        tracked = _run(git_repo, "ls-files").stdout.splitlines()
        assert "brand_new.py" in tracked


class TestEnsureBranchHasCommits:
    @pytest.mark.asyncio
    async def test_passes_when_branch_has_commits(self, git_repo):
        _write(git_repo, "change.py", "x\n")
        _run(git_repo, "add", "-A")
        _run(git_repo, "commit", "-m", "real work")

        # Should not raise
        await ensure_branch_has_commits(git_repo, base_branch="main")

    @pytest.mark.asyncio
    async def test_raises_on_empty_branch(self, git_repo):
        """This is the task-48 failure mode: branch == base, nothing to PR."""
        with pytest.raises(EmptyBranchError) as exc:
            await ensure_branch_has_commits(git_repo, base_branch="main")
        # Error message should be actionable
        assert "no commits" in str(exc.value).lower() or "main" in str(exc.value)

    @pytest.mark.asyncio
    async def test_passes_if_multiple_commits_on_branch(self, git_repo):
        _write(git_repo, "a.py", "1\n")
        _run(git_repo, "add", "-A")
        _run(git_repo, "commit", "-m", "c1")
        _write(git_repo, "b.py", "2\n")
        _run(git_repo, "add", "-A")
        _run(git_repo, "commit", "-m", "c2")

        await ensure_branch_has_commits(git_repo, base_branch="main")


class TestEmptyBranchError:
    def test_is_a_runtime_error(self):
        """EmptyBranchError should be a specific exception type we can catch."""
        err = EmptyBranchError("feature/x has no commits relative to main")
        assert isinstance(err, RuntimeError)
