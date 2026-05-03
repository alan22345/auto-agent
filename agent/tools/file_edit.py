"""String-replacement file editing (old_string -> new_string)."""

from __future__ import annotations

import os
from typing import Any

from agent.tools.base import Tool, ToolContext, ToolResult


class FileEditTool(Tool):
    name = "file_edit"
    description = (
        "Edit a file by replacing an exact string with a new string. "
        "The old_string must appear exactly once in the file (for safety). "
        "Use replace_all=true to replace every occurrence."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Path to the file to edit.",
            },
            "old_string": {
                "type": "string",
                "description": "The exact text to find and replace.",
            },
            "new_string": {
                "type": "string",
                "description": "The replacement text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences (default false).",
                "default": False,
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    }
    is_readonly = False

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.readonly:
            return ToolResult(output="Error: file_edit is disabled in read-only mode.", is_error=True)

        file_path = arguments["file_path"]
        old_string = arguments["old_string"]
        new_string = arguments["new_string"]
        replace_all = arguments.get("replace_all", False)

        resolved = context.resolve(file_path)
        if resolved is None:
            return ToolResult(output=f"Error: path '{file_path}' escapes the workspace.", is_error=True)

        if not os.path.isfile(resolved):
            return ToolResult(output=f"Error: file not found: {file_path}", is_error=True)

        try:
            with open(resolved, "r") as f:
                content = f.read()
        except Exception as e:
            return ToolResult(output=f"Error reading file: {e}", is_error=True)

        if old_string == new_string:
            return ToolResult(output="Error: old_string and new_string are identical.", is_error=True)

        count = content.count(old_string)
        if count == 0:
            return ToolResult(
                output=f"Error: old_string not found in {file_path}. Ensure exact match including whitespace.",
                is_error=True,
            )

        if count > 1 and not replace_all:
            return ToolResult(
                output=(
                    f"Error: old_string found {count} times in {file_path}. "
                    "Provide more context to make it unique, or set replace_all=true."
                ),
                is_error=True,
            )

        if replace_all:
            new_content = content.replace(old_string, new_string)
            replaced = count
        else:
            new_content = content.replace(old_string, new_string, 1)
            replaced = 1

        try:
            with open(resolved, "w") as f:
                f.write(new_content)
        except Exception as e:
            return ToolResult(output=f"Error writing file: {e}", is_error=True)

        return ToolResult(output=f"Replaced {replaced} occurrence(s) in {file_path}")
