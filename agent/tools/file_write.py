"""Write or create a file."""

from __future__ import annotations

import os
from typing import Any

from agent.tools.base import Tool, ToolContext, ToolResult


class FileWriteTool(Tool):
    name = "file_write"
    description = (
        "Write content to a file, creating it (and parent directories) if needed. "
        "Overwrites existing files. Use file_edit for partial modifications."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to write (relative to workspace root or absolute).",
            },
            "content": {
                "type": "string",
                "description": "Full file content to write.",
            },
        },
        "required": ["file_path", "content"],
    }
    is_readonly = False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.readonly:
            return ToolResult(output="Error: file_write is disabled in read-only mode.", is_error=True)

        file_path = arguments["file_path"]
        content = arguments["content"]

        resolved = context.resolve(file_path)
        if resolved is None:
            return ToolResult(output=f"Error: path '{file_path}' escapes the workspace.", is_error=True)

        try:
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            with open(resolved, "w") as f:
                f.write(content)
        except Exception as e:
            return ToolResult(output=f"Error writing file: {e}", is_error=True)

        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return ToolResult(output=f"Wrote {line_count} lines to {file_path}")
