"""Shell command execution with timeout."""

from __future__ import annotations

from typing import Any

from agent import sh
from agent.tools.base import Tool, ToolContext, ToolResult


class BashTool(Tool):
    name = "bash"
    description = (
        "Execute a shell command in the workspace directory. "
        "Returns stdout and stderr. Use for running tests, builds, "
        "installs, and other terminal operations."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds. Default 120.",
                "default": 120,
            },
        },
        "required": ["command"],
    }
    is_readonly = False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.readonly:
            return ToolResult(output="Error: bash is disabled in read-only mode.", is_error=True)

        command = arguments["command"]
        timeout = min(arguments.get("timeout", 120), 600)  # Cap at 10 minutes

        try:
            result = await sh.run_shell(
                command,
                cwd=context.workspace,
                timeout=timeout,
                max_output=100_000,
            )
        except Exception as e:
            return ToolResult(output=f"Error executing command: {e}", is_error=True)

        if result.timed_out:
            return ToolResult(
                output=f"Command timed out after {timeout}s: {command}",
                is_error=True,
            )

        parts: list[str] = []
        if result.stdout.strip():
            parts.append(result.stdout.rstrip())
        if result.stderr.strip():
            parts.append(f"STDERR:\n{result.stderr.rstrip()}")
        if result.returncode != 0:
            parts.append(f"Exit code: {result.returncode}")

        output = "\n".join(parts) if parts else "(no output)"

        return ToolResult(
            output=output,
            token_estimate=len(output) // 3,
            is_error=result.failed,
        )
