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
    """Create a fresh git repo with a base commit and a feature branch.

    Deliberately does NOT set user.name / user.email on the repo — so that
    tests catch the "Author identity unknown" failure mode that killed task 49.
    """
    tmp = tempfile.mkdtemp(prefix="wcs-test-")

    # Sanitize env: strip any ambient GIT_AUTHOR_* / GIT_COMMITTER_*, and point
    # HOME/XDG_CONFIG_HOME at an empty dir so we don't inherit a global identity.
    empty_home = tempfile.mkdtemp(prefix="wcs-home-")
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith("GIT_AUTHOR") and not k.startswith("GIT_COMMITTER")
    }
    env["HOME"] = empty_home
    env["XDG_CONFIG_HOME"] = os.path.join(empty_home, ".config")
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"   # Disable system gitconfig (e.g. /etc/gitconfig)

    def run(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=tmp, capture_output=True, text=True, check=check, env=env,
        )

    # Seed the base commit WITH identity via env-var fallback, since init needs
    # at least one commit for the tests to operate on. After this the repo has
    # no configured user.name/email — subsequent commits will fail until we set it.
    seed_env = {**env, "GIT_AUTHOR_NAME": "seed", "GIT_AUTHOR_EMAIL": "seed@seed",
                "GIT_COMMITTER_NAME": "seed", "GIT_COMMITTER_EMAIL": "seed@seed"}
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp, capture_output=True, check=True, env=env)
    open(os.path.join(tmp, "README.md"), "w").write("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp, capture_output=True, check=True, env=env)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp, capture_output=True, check=True, env=seed_env)
    subprocess.run(["git", "checkout", "-b", "feature/test"], cwd=tmp, capture_output=True, check=True, env=env)

    # Store the sanitized env on the yielded path so tests can use it in _run()
    yield tmp, env

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(empty_home, ignore_errors=True)


def _write(path: str, name: str, content: str):
    with open(os.path.join(path, name), "w") as f:
        f.write(content)


def _run(cwd: str, *args, env=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        env=env or os.environ,
    )


class TestCommitPendingChanges:
    @pytest.mark.asyncio
    async def test_commits_uncommitted_files(self, git_repo):
        """Reproduces task 48: agent wrote files but didn't commit."""
        tmp, env = git_repo
        _write(tmp, "app.py", "print('hello')\n")
        _write(tmp, "models.py", "class Foo: pass\n")

        status = _run(tmp, "status", "--porcelain", env=env).stdout
        assert "app.py" in status and "models.py" in status

        committed = await commit_pending_changes(
            tmp, task_id=48, title="Fix agent cloning bug",
        )
        assert committed is True

        status = _run(tmp, "status", "--porcelain", env=env).stdout
        assert status.strip() == ""

        last_msg = _run(tmp, "log", "-1", "--format=%s", env=env).stdout.strip()
        assert "#48" in last_msg
        assert "Fix agent cloning bug" in last_msg

    @pytest.mark.asyncio
    async def test_commits_without_preconfigured_git_identity(self, git_repo):
        """Reproduces task 49: git has no user.name / user.email set in the repo
        or globally. Our commit_pending_changes must still succeed by supplying
        an identity. Previously raised 'Author identity unknown'."""
        tmp, env = git_repo
        _write(tmp, "late.py", "oops I forgot to commit\n")
        committed = await commit_pending_changes(tmp, 49, "Forgotten commit")
        assert committed is True
        # Verify commit actually has an author
        author = _run(tmp, "log", "-1", "--format=%an <%ae>", env=env).stdout.strip()
        assert "<" in author and ">" in author

    @pytest.mark.asyncio
    async def test_noop_when_nothing_to_commit(self, git_repo):
        """If the agent already committed, commit_pending_changes is a no-op."""
        tmp, env = git_repo
        # Seed the commit with explicit env-var identity (simulates agent committing)
        _write(tmp, "app.py", "x\n")
        seed_env = {**env, "GIT_AUTHOR_NAME": "agent", "GIT_AUTHOR_EMAIL": "a@a",
                    "GIT_COMMITTER_NAME": "agent", "GIT_COMMITTER_EMAIL": "a@a"}
        _run(tmp, "add", "-A", env=env)
        _run(tmp, "commit", "-m", "agent committed", env=seed_env)

        before_sha = _run(tmp, "rev-parse", "HEAD", env=env).stdout.strip()
        committed = await commit_pending_changes(tmp, 48, "anything")
        after_sha = _run(tmp, "rev-parse", "HEAD", env=env).stdout.strip()

        assert committed is False
        assert before_sha == after_sha, "Should not create empty commits"

    @pytest.mark.asyncio
    async def test_commits_untracked_files_too(self, git_repo):
        """New files the agent created (never git-add'd) must be captured."""
        tmp, env = git_repo
        _write(tmp, "brand_new.py", "new\n")
        committed = await commit_pending_changes(tmp, 48, "add new file")
        assert committed is True
        tracked = _run(tmp, "ls-files", env=env).stdout.splitlines()
        assert "brand_new.py" in tracked


class TestEnsureBranchHasCommits:
    @pytest.mark.asyncio
    async def test_passes_when_branch_has_commits(self, git_repo):
        tmp, env = git_repo
        _write(tmp, "change.py", "x\n")
        seed_env = {**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        _run(tmp, "add", "-A", env=env)
        _run(tmp, "commit", "-m", "real work", env=seed_env)

        await ensure_branch_has_commits(tmp, base_branch="main")

    @pytest.mark.asyncio
    async def test_raises_on_empty_branch(self, git_repo):
        """This is the task-48 failure mode: branch == base, nothing to PR."""
        tmp, _ = git_repo
        with pytest.raises(EmptyBranchError) as exc:
            await ensure_branch_has_commits(tmp, base_branch="main")
        assert "no commits" in str(exc.value).lower() or "main" in str(exc.value)

    @pytest.mark.asyncio
    async def test_passes_if_multiple_commits_on_branch(self, git_repo):
        tmp, env = git_repo
        seed_env = {**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        _write(tmp, "a.py", "1\n")
        _run(tmp, "add", "-A", env=env)
        _run(tmp, "commit", "-m", "c1", env=seed_env)
        _write(tmp, "b.py", "2\n")
        _run(tmp, "add", "-A", env=env)
        _run(tmp, "commit", "-m", "c2", env=seed_env)

        await ensure_branch_has_commits(tmp, base_branch="main")


class TestEmptyBranchError:
    def test_is_a_runtime_error(self):
        """EmptyBranchError should be a specific exception type we can catch."""
        err = EmptyBranchError("feature/x has no commits relative to main")
        assert isinstance(err, RuntimeError)
