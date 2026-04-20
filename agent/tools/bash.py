"""Shell command execution with timeout."""

from __future__ import annotations

import asyncio
import os
from typing import Any

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
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=context.workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return ToolResult(
                    output=f"Command timed out after {timeout}s: {command}",
                    is_error=True,
                )

            stdout_str = (stdout or b"").decode(errors="replace")
            stderr_str = (stderr or b"").decode(errors="replace")

            parts: list[str] = []
            if stdout_str.strip():
                parts.append(stdout_str.rstrip())
            if stderr_str.strip():
                parts.append(f"STDERR:\n{stderr_str.rstrip()}")
            if proc.returncode != 0:
                parts.append(f"Exit code: {proc.returncode}")

            output = "\n".join(parts) if parts else "(no output)"

            # Truncate very large outputs
            max_chars = 100_000
            if len(output) > max_chars:
                output = output[:max_chars] + f"\n... (truncated, {len(output)} total chars)"

            return ToolResult(
                output=output,
                token_estimate=len(output) // 3,
                is_error=proc.returncode != 0,
            )

        except Exception as e:
            return ToolResult(output=f"Error executing command: {e}", is_error=True)
