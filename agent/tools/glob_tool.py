"""File pattern matching using glob."""

from __future__ import annotations

import os
import pathlib
from typing import Any

from agent.tools.base import Tool, ToolContext, ToolResult


class GlobTool(Tool):
    name = "glob"
    description = (
        "Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). "
        "Returns matching file paths sorted by modification time (newest first)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match files against.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in (relative to workspace). Default: workspace root.",
            },
        },
        "required": ["pattern"],
    }
    is_readonly = True

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        pattern = arguments["pattern"]
        search_dir = arguments.get("path", "")

        base = os.path.join(context.workspace, search_dir) if search_dir else context.workspace
        base = os.path.realpath(base)
        ws_real = os.path.realpath(context.workspace)
        if not base.startswith(ws_real):
            return ToolResult(output="Error: path escapes the workspace.", is_error=True)

        try:
            matches = list(pathlib.Path(base).glob(pattern))
        except Exception as e:
            return ToolResult(output=f"Error: {e}", is_error=True)

        # Filter out hidden dirs and common noise
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv"}
        filtered = []
        for p in matches:
            if p.is_file() and not any(part in skip for part in p.parts):
                filtered.append(p)

        # Sort by mtime descending
        filtered.sort(key=lambda p: p.stat().st_mtime, reverse=True)

        # Cap results
        max_results = 250
        truncated = len(filtered) > max_results
        filtered = filtered[:max_results]

        # Format as relative paths
        lines = []
        for p in filtered:
            try:
                rel = p.relative_to(ws_real)
            except ValueError:
                rel = p
            lines.append(str(rel))

        if not lines:
            return ToolResult(output=f"No files matching '{pattern}' found.")

        output = "\n".join(lines)
        if truncated:
            output += f"\n... (truncated, showing first {max_results} of {len(matches)} matches)"
        return ToolResult(output=output, token_estimate=len(output) // 4)
