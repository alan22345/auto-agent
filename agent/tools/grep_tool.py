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
        "with file paths and line numbers. Supports glob filtering, context lines, "
        "and multiline patterns."
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
            "context_lines": {
                "type": "integer",
                "description": "Number of lines to show before and after each match (like grep -C). Default 0.",
                "default": 0,
            },
            "multiline": {
                "type": "boolean",
                "description": "Enable multiline mode where . matches newlines and patterns can span lines. Default false.",
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
        context_lines = min(arguments.get("context_lines", 0), 10)  # Cap at 10
        multiline = arguments.get("multiline", False)

        base = os.path.join(context.workspace, search_path) if search_path else context.workspace
        base = os.path.realpath(base)
        ws_real = os.path.realpath(context.workspace)
        if not base.startswith(ws_real):
            return ToolResult(output="Error: path escapes the workspace.", is_error=True)

        flags = re.IGNORECASE if case_insensitive else 0
        if multiline:
            flags |= re.MULTILINE | re.DOTALL
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

            try:
                rel = fp.relative_to(ws_real)
            except ValueError:
                rel = fp

            if multiline:
                # Multiline search: find spans across lines
                for m in regex.finditer(text):
                    if len(matches) >= max_matches:
                        break
                    start_line = text[:m.start()].count("\n") + 1
                    end_line = text[:m.end()].count("\n") + 1
                    matched_text = m.group()
                    if len(matched_text) > 500:
                        matched_text = matched_text[:500] + "..."
                    matches.append(f"{rel}:{start_line}-{end_line}: {matched_text}")
            else:
                # Line-by-line search with optional context
                lines = text.splitlines()
                matched_line_nums: set[int] = set()

                for line_num, line in enumerate(lines):
                    if regex.search(line):
                        matched_line_nums.add(line_num)

                if not matched_line_nums:
                    continue

                # Build output with context lines
                if context_lines > 0:
                    # Group nearby matches to avoid overlapping context
                    shown_lines: set[int] = set()
                    for ln in sorted(matched_line_nums):
                        start = max(0, ln - context_lines)
                        end = min(len(lines), ln + context_lines + 1)
                        for i in range(start, end):
                            shown_lines.add(i)

                    # Output grouped context blocks
                    prev_ln = -2
                    for ln in sorted(shown_lines):
                        if len(matches) >= max_matches:
                            break
                        if ln - prev_ln > 1 and prev_ln >= 0:
                            matches.append("--")  # Context separator
                        prefix = ">" if ln in matched_line_nums else " "
                        matches.append(f"{rel}:{ln + 1}:{prefix} {lines[ln].rstrip()}")
                        prev_ln = ln
                else:
                    for ln in sorted(matched_line_nums):
                        if len(matches) >= max_matches:
                            break
                        matches.append(f"{rel}:{ln + 1}: {lines[ln].rstrip()}")

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
