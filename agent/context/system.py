"""System prompt builder — assembles git state, CLAUDE.md, and repo summary."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import structlog

logger = structlog.get_logger()

# Cap git status output
_GIT_STATUS_MAX_CHARS = 2000

BASE_AGENT_INSTRUCTIONS = """\
You are an autonomous coding agent. You have access to tools for reading, \
writing, editing files, searching code, and running shell commands.

## Rules
- Follow the repository's existing code style and patterns.
- Run tests after making changes to verify correctness.
- Do not introduce new dependencies unless necessary.
- Do not refactor unrelated code — keep changes focused.
- Commit with clear messages explaining what changed and why.
- For bug fixes, identify and fix the ROOT CAUSE, not just the symptom.
- No hardcoded secrets, tokens, or credentials.
- Validate inputs at system boundaries.
"""


class SystemPromptBuilder:
    """Builds the system prompt from workspace context."""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    async def build(
        self,
        workspace: str,
        repo_summary: str | None = None,
        extra_instructions: str | None = None,
    ) -> str:
        """Build the full system prompt.

        Concatenates: base instructions + CLAUDE.md + git context + repo summary + date.
        """
        parts: list[str] = [BASE_AGENT_INSTRUCTIONS]

        # CLAUDE.md
        claude_md = await self._read_claude_md(workspace)
        if claude_md:
            parts.append(f"## Repository instructions (CLAUDE.md)\n{claude_md}")

        # Git context
        git_context = await self._git_context(workspace)
        if git_context:
            parts.append(f"## Current git state\n{git_context}")

        # Repo summary
        if repo_summary:
            parts.append(f"## Repo summary\n{repo_summary}")

        # Extra instructions (e.g., from task-specific prompts)
        if extra_instructions:
            parts.append(extra_instructions)

        # Date
        parts.append(f"Current date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")

        return "\n\n".join(parts)

    def invalidate_cache(self) -> None:
        """Clear cached values (call at the start of each new agent run)."""
        self._cache.clear()

    async def _read_claude_md(self, workspace: str) -> str | None:
        """Read CLAUDE.md from workspace root if it exists."""
        cache_key = f"claude_md:{workspace}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        claude_md_path = os.path.join(workspace, "CLAUDE.md")
        if not os.path.isfile(claude_md_path):
            self._cache[cache_key] = ""
            return None

        try:
            with open(claude_md_path, "r") as f:
                content = f.read()
            self._cache[cache_key] = content
            return content
        except Exception as e:
            logger.warning("read_claude_md_failed", error=str(e))
            return None

    async def _git_context(self, workspace: str) -> str | None:
        """Get current branch, status, and recent commits."""
        cache_key = f"git:{workspace}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if not os.path.isdir(os.path.join(workspace, ".git")):
            return None

        parts: list[str] = []

        # Current branch
        branch = await self._run_git("branch", "--show-current", cwd=workspace)
        if branch:
            parts.append(f"Branch: {branch.strip()}")

        # Status (capped)
        status = await self._run_git("status", "--short", cwd=workspace)
        if status:
            if len(status) > _GIT_STATUS_MAX_CHARS:
                status = status[:_GIT_STATUS_MAX_CHARS] + "\n... (truncated)"
            parts.append(f"Status:\n{status.strip()}")

        # Recent commits
        log = await self._run_git("log", "--oneline", "-5", cwd=workspace)
        if log:
            parts.append(f"Recent commits:\n{log.strip()}")

        result = "\n".join(parts) if parts else None
        if result:
            self._cache[cache_key] = result
        return result

    @staticmethod
    async def _run_git(*args: str, cwd: str) -> str:
        """Run a git command, returning stdout or empty string on failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return (stdout or b"").decode(errors="replace")
        except Exception:
            return ""
