"""Shared git/workspace helpers for the eval providers.

Both ``agent_provider`` and ``claude_cli_provider`` set up a throwaway git
workspace the same way (init + empty initial commit) and capture
agent-committed changes the same way (``HEAD~1..HEAD``). Keeping the logic here
avoids the two providers drifting apart.
"""

import asyncio
import os

_PIPE = asyncio.subprocess.PIPE


async def init_git_workspace(workspace):
    """Initialise a git repo in *workspace* and make an empty initial commit.

    ``git init`` → ``git add -A`` → an ``--allow-empty`` initial commit using a
    fixed eval identity so the committer is deterministic across runs.
    """
    for cmd in (["git", "init"], ["git", "add", "-A"]):
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=workspace, stdout=_PIPE, stderr=_PIPE,
        )
        await proc.communicate()
    proc = await asyncio.create_subprocess_exec(
        "git", "commit", "-m", "initial", "--allow-empty",
        cwd=workspace,
        stdout=_PIPE, stderr=_PIPE,
        env={**os.environ, "GIT_AUTHOR_NAME": "eval", "GIT_AUTHOR_EMAIL": "eval@test",
             "GIT_COMMITTER_NAME": "eval", "GIT_COMMITTER_EMAIL": "eval@test"},
    )
    await proc.communicate()


async def capture_committed_diff(workspace):
    """Return the diff of the last commit (``HEAD~1..HEAD``) as bytes.

    Empty bytes when there is no commit history yet. Captures changes an agent
    committed rather than left in the working tree.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "log", "--oneline", "-1", "--format=%H", cwd=workspace,
        stdout=_PIPE, stderr=_PIPE,
    )
    head_hash, _ = await proc.communicate()

    committed_diff = b""
    if head_hash.strip():
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "HEAD~1..HEAD", cwd=workspace,
            stdout=_PIPE, stderr=_PIPE,
        )
        committed_diff, _ = await proc.communicate()
    return committed_diff
