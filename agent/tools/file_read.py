"""Read file contents with optional line range."""

from __future__ import annotations

import os
from typing import Any

from agent.tools.base import Tool, ToolContext, ToolResult


class FileReadTool(Tool):
    name = "file_read"
    description = (
        "Read a file's contents. Returns lines with line numbers (like cat -n). "
        "Use offset and limit to read specific portions of large files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file (relative to workspace root or absolute).",
            },
            "offset": {
                "type": "integer",
                "description": "Line number to start reading from (0-based). Default 0.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read. Default 2000.",
            },
        },
        "required": ["file_path"],
    }
    is_readonly = True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        file_path = arguments["file_path"]
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", 2000)

        resolved = self._resolve_path(file_path, context.workspace)
        if not resolved:
            return ToolResult(
                output=f"Error: path '{file_path}' escapes the workspace.",
                is_error=True,
            )

        if not os.path.isfile(resolved):
            return ToolResult(
                output=f"Error: file not found: {file_path}",
                is_error=True,
            )

        try:
            with open(resolved, "r", errors="replace") as f:
                all_lines = f.readlines()
        except Exception as e:
            return ToolResult(output=f"Error reading file: {e}", is_error=True)

        selected = all_lines[offset : offset + limit]
        numbered = []
        for i, line in enumerate(selected, start=offset + 1):
            numbered.append(f"{i}\t{line.rstrip()}")

        output = "\n".join(numbered)
        if not output:
            output = "(empty file)" if not all_lines else "(no lines in selected range)"

        return ToolResult(
            output=output,
            token_estimate=len(output) // 3,
        )

    @staticmethod
    def _resolve_path(file_path: str, workspace: str) -> str | None:
        """Resolve a file path relative to workspace, preventing traversal."""
        if os.path.isabs(file_path):
            resolved = os.path.realpath(file_path)
        else:
            resolved = os.path.realpath(os.path.join(workspace, file_path))

        ws_real = os.path.realpath(workspace)
        if not resolved.startswith(ws_real + os.sep) and resolved != ws_real:
            return None
        return resolved
