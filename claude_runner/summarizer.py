"""Repo summarizer — generates and caches a summary of a repo's codebase."""

from __future__ import annotations

import asyncio

from claude_runner.workspace import WORKSPACES_DIR, clone_repo


SUMMARIZE_PROMPT = """\
Provide a concise summary of this repository for a developer who needs to work on it. Include:

1. **Tech stack**: Languages, frameworks, key dependencies
2. **Project structure**: Main directories and what they contain
3. **Key files**: Entry points, config files, important modules
4. **Patterns & conventions**: Coding style, naming conventions, architecture patterns
5. **How to build/test/run**: Commands for development workflow
6. **Domain context**: What the project does, key domain concepts

Keep it under 2000 words. Focus on what a developer needs to start making changes quickly.
Output as plain text, not to any file.
"""


async def generate_repo_summary(repo_url: str, repo_name: str, default_branch: str = "main") -> str:
    """Clone a repo (or reuse existing checkout) and generate a summary using Claude."""
    workspace = await clone_repo(repo_url, task_id=0, default_branch=default_branch)

    proc = await asyncio.create_subprocess_exec(
        "claude", "--print", "--dangerously-skip-permissions", SUMMARIZE_PROMPT,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ""

    output = (stdout or b"").decode().strip()
    if not output:
        output = (stderr or b"").decode().strip()

    return output
