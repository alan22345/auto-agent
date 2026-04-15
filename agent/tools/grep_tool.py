"""Content search using regex."""

from __future__ import annotations

import os
import pathlib
import re
from typing import Any

from agent.tools.base import Tool, ToolContext, ToolResult


class GrepTool(Tool):
    name = "grep"
    description = (
        "Search file contents using a regex pattern. Returns matching lines "
        "with file paths and line numbers. Supports glob filtering."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for.",
            },
            "path": {
                "type": "string",
                "description": "File or directory to search in (relative to workspace). Default: workspace root.",
            },
            "glob": {
                "type": "string",
                "description": "Glob pattern to filter files (e.g. '*.py', '*.ts').",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case-insensitive search. Default false.",
                "default": False,
            },
        },
        "required": ["pattern"],
    }
    is_readonly = True

    # Directories to skip
    _SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".ruff_cache"}
    # Binary extensions to skip
    _SKIP_EXT = {".pyc", ".pyo", ".so", ".o", ".a", ".dll", ".exe", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot"}

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        pattern_str = arguments["pattern"]
        search_path = arguments.get("path", "")
        file_glob = arguments.get("glob")
        case_insensitive = arguments.get("case_insensitive", False)

        base = os.path.join(context.workspace, search_path) if search_path else context.workspace
        base = os.path.realpath(base)
        ws_real = os.path.realpath(context.workspace)
        if not base.startswith(ws_real):
            return ToolResult(output="Error: path escapes the workspace.", is_error=True)

        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern_str, flags)
        except re.error as e:
            return ToolResult(output=f"Error: invalid regex: {e}", is_error=True)

        # Collect files to search
        if os.path.isfile(base):
            files = [pathlib.Path(base)]
        else:
            files = self._collect_files(pathlib.Path(base), file_glob)

        matches: list[str] = []
        max_matches = 500
        for fp in files:
            if len(matches) >= max_matches:
                break
            try:
                text = fp.read_text(errors="replace")
            except Exception:
                continue
            for line_num, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    try:
                        rel = fp.relative_to(ws_real)
                    except ValueError:
                        rel = fp
                    matches.append(f"{rel}:{line_num}: {line.rstrip()}")
                    if len(matches) >= max_matches:
                        break

        if not matches:
            return ToolResult(output=f"No matches for pattern '{pattern_str}'.")

        output = "\n".join(matches)
        if len(matches) >= max_matches:
            output += f"\n... (showing first {max_matches} matches)"
        return ToolResult(output=output, token_estimate=len(output) // 3)

    def _collect_files(self, directory: pathlib.Path, file_glob: str | None) -> list[pathlib.Path]:
        """Recursively collect files, respecting skip lists and optional glob filter."""
        result: list[pathlib.Path] = []
        max_files = 5000
        try:
            for item in directory.rglob(file_glob or "*"):
                if len(result) >= max_files:
                    break
                if any(skip in item.parts for skip in self._SKIP_DIRS):
                    continue
                if item.is_file() and item.suffix not in self._SKIP_EXT:
                    result.append(item)
        except PermissionError:
            pass
        return result
