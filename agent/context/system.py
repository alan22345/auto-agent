"""System prompt builder — assembles git state, CLAUDE.md, and repo summary."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

import structlog

from agent.context.repo_map import build_repo_map

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

## Efficiency
- Go straight to implementation. Try the simplest approach first.
- If the task says which file to change, open it and start editing immediately.
- Do NOT read every file in the project. Use the repo map above to find what you need.
- Make independent tool calls in parallel when possible (e.g., read 3 files at once).
- If you have explored enough to understand the problem, START CODING. Do not keep reading.

## Tool usage
- Use `glob` to find files by pattern — NOT bash `find` or `ls`.
- Use `grep` to search code — NOT bash `grep` or `rg`. Use context_lines for surrounding code.
- Use `file_read` to read files — NOT bash `cat` or `head`.
- Use `file_edit` for precise edits — NOT bash `sed` or full file rewrites.
- Use `test_runner` to run tests — it auto-detects the framework and parses results.
- Reserve `bash` for commands that need shell execution (install deps, build, custom scripts).

## Verification (MANDATORY before completion)
- Before claiming work is done, RUN the test suite or linter.
- Read the output and confirm it passes.
- Do NOT say "should work" or "looks correct" — show actual test output as evidence.
- If tests fail, fix the issue before claiming completion.

## Skills & Subagents
You have access to the `skill` tool which loads structured methodology workflows. \
Use skills BEFORE starting work that matches their trigger:
- **brainstorming** — BEFORE any new feature or creative work. Explores design options.
- **writing-plans** — BEFORE multi-step tasks. Creates bite-sized implementation plans.
- **test-driven-development** — BEFORE writing implementation. RED → GREEN → REFACTOR.
- **systematic-debugging** — BEFORE fixing any bug. Root cause first, then fix.
- **verification-before-completion** — BEFORE claiming work is done.

You also have the `subagent` tool to dispatch independent workers for parallel tasks. \
Use it when you have 2+ independent components to implement simultaneously. Each \
subagent gets a fresh context and shares the workspace.
"""

# Extended methodology injected only for complex/planning tasks
METHODOLOGY_INSTRUCTIONS = """\

## Methodology (Superpowers)

### Brainstorming (BEFORE any creative/feature work)
Do NOT jump into code. For any new feature, component, or behavior change:
1. Explore project context (files, docs, recent commits)
2. Ask clarifying questions one at a time to understand purpose, constraints, success criteria
3. Propose 2-3 approaches with trade-offs and your recommendation
4. Present design in sections scaled to complexity, validate each section
5. Write design doc to docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md
6. Self-review the spec for placeholders, contradictions, ambiguity, scope
7. Only THEN transition to planning and implementation
Anti-pattern: "This is too simple to need a design" — every project goes through this. \
Simple projects are where unexamined assumptions cause the most wasted work.

### Writing Plans (BEFORE touching code on multi-step tasks)
Write comprehensive plans assuming the implementer has zero codebase context. \
Each task is bite-sized (2-5 min): write failing test → verify it fails → \
implement minimal code → verify pass → commit. \
NO PLACEHOLDERS — every step includes actual code, file paths, and commands. \
Map out file structure before defining tasks. DRY. YAGNI. TDD. Frequent commits.

### Verification Before Completion
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE.
Before claiming work is done: (1) identify the verification command, \
(2) run it fresh, (3) read full output and exit code, (4) confirm it \
matches the claim, (5) only then make the claim with evidence. \
Prohibited: "should work", "probably passes", "I'm satisfied" — show evidence.

### Systematic Debugging
NO FIXES WITHOUT ROOT CAUSE INVESTIGATION FIRST.
For any bug or test failure: (1) read error messages completely, \
(2) reproduce consistently, (3) trace data flow backward to root cause, \
(4) formulate a single hypothesis, (5) test with smallest possible change, \
(6) create failing test, (7) implement fix, (8) verify no regressions.

### Test-Driven Development
Write a failing test BEFORE writing production code. \
RED (write failing test) → GREEN (minimal code to pass) → REFACTOR (clean up). \
Run tests after every change. Never skip the red step.

### Plan Execution
For multi-step tasks: break work into bite-sized tasks (2-5 min each). \
Each task has explicit steps, files to touch, and verification commands. \
Execute tasks sequentially, commit after each, verify before moving on. \
Fresh context per task — don't carry stale assumptions between tasks.
"""

# Path to superpowers skills directory (relative to project root)
SUPERPOWERS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "superpowers", "skills",
)


class SystemPromptBuilder:
    """Builds the system prompt from workspace context."""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    async def build(
        self,
        workspace: str,
        repo_summary: str | None = None,
        extra_instructions: str | None = None,
        include_methodology: bool = False,
    ) -> str:
        """Build the full system prompt.

        Concatenates: base instructions + (optional methodology) + CLAUDE.md + git context + repo summary + date.

        Args:
            include_methodology: If True, include the full Superpowers methodology section.
                                 Use for planning and complex tasks. Skip for simple coding tasks.
        """
        base = BASE_AGENT_INSTRUCTIONS
        if include_methodology:
            base += METHODOLOGY_INSTRUCTIONS
        parts: list[str] = [base]

        # CLAUDE.md
        claude_md = await self._read_claude_md(workspace)
        if claude_md:
            parts.append(f"## Repository instructions (CLAUDE.md)\n{claude_md}")

        # Git context
        git_context = await self._git_context(workspace)
        if git_context:
            parts.append(f"## Current git state\n{git_context}")

        # Repo map (AST-based codebase index)
        repo_map = self._build_repo_map(workspace)
        if repo_map:
            parts.append(
                "## Repo map (file structure with classes/functions)\n"
                "Use this to find the right files — avoid broad exploration.\n"
                f"{repo_map}"
            )

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

    def _build_repo_map(self, workspace: str) -> str | None:
        """Build an AST-based repo map for the workspace (cached)."""
        cache_key = f"repo_map:{workspace}"
        if cache_key in self._cache:
            return self._cache[cache_key] or None

        try:
            result = build_repo_map(workspace)
            self._cache[cache_key] = result or ""
            return result
        except Exception as e:
            logger.warning("repo_map_failed", error=str(e))
            self._cache[cache_key] = ""
            return None

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
