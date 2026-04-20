"""Git operations wrapper."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from agent.tools.base import Tool, ToolContext, ToolResult


class GitTool(Tool):
    name = "git"
    description = (
        "Run git commands in the workspace. Supports: status, diff, log, "
        "branch, add, commit, show. For safety, destructive operations "
        "(reset --hard, push --force) are blocked."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Git subcommand and arguments (e.g. 'status', 'diff --staged', "
                    "'log --oneline -10', 'add .', 'commit -m \"message\"')."
                ),
            },
        },
        "required": ["command"],
    }
    is_readonly = True  # Read operations are safe; we block destructive ones below

    # Commands that are always safe
    _SAFE_COMMANDS = {"status", "diff", "log", "show", "branch", "rev-parse", "ls-files", "shortlog", "blame", "tag"}
    # Commands allowed only in non-readonly mode
    _WRITE_COMMANDS = {"add", "commit", "checkout", "switch", "stash", "merge", "rebase", "cherry-pick", "rm", "mv"}
    # Always blocked
    _BLOCKED_PATTERNS = {"push --force", "push -f", "reset --hard", "clean -f", "clean -fd"}

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        command = arguments["command"].strip()

        # Parse the subcommand
        parts = command.split()
        if not parts:
            return ToolResult(output="Error: empty git command.", is_error=True)
        subcommand = parts[0]

        # Block dangerous patterns
        for pattern in self._BLOCKED_PATTERNS:
            if pattern in command:
                return ToolResult(
                    output=f"Error: '{pattern}' is blocked for safety.",
                    is_error=True,
                )

        # Block push entirely (orchestrator handles pushing)
        if subcommand == "push":
            return ToolResult(
                output="Error: git push is handled by the orchestrator, not the agent.",
                is_error=True,
            )

        # Check readonly mode for write commands
        if context.readonly and subcommand in self._WRITE_COMMANDS:
            return ToolResult(
                output=f"Error: git {subcommand} is disabled in read-only mode.",
                is_error=True,
            )

        # Execute
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *parts,
                cwd=context.workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            return ToolResult(output="Error: git command timed out.", is_error=True)
        except Exception as e:
            return ToolResult(output=f"Error: {e}", is_error=True)

        stdout_str = (stdout or b"").decode(errors="replace")
        stderr_str = (stderr or b"").decode(errors="replace")

        output_parts: list[str] = []
        if stdout_str.strip():
            output_parts.append(stdout_str.rstrip())
        if stderr_str.strip() and proc.returncode != 0:
            output_parts.append(f"STDERR: {stderr_str.rstrip()}")

        output = "\n".join(output_parts) if output_parts else "(no output)"

        return ToolResult(
            output=output,
            token_estimate=len(output) // 4,
            is_error=proc.returncode != 0,
        )
