"""Git operations wrapper."""

from __future__ import annotations

from typing import Any

from agent import sh
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

        try:
            result = await sh.run(
                ["git", *parts],
                cwd=context.workspace,
                timeout=30,
            )
        except Exception as e:
            return ToolResult(output=f"Error: {e}", is_error=True)

        if result.timed_out:
            return ToolResult(output="Error: git command timed out.", is_error=True)

        output_parts: list[str] = []
        if result.stdout.strip():
            output_parts.append(result.stdout.rstrip())
        if result.stderr.strip() and result.returncode != 0:
            output_parts.append(f"STDERR: {result.stderr.rstrip()}")

        output = "\n".join(output_parts) if output_parts else "(no output)"

        return ToolResult(
            output=output,
            token_estimate=len(output) // 4,
            is_error=result.failed,
        )
