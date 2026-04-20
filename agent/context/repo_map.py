"""Repo map builder — lightweight AST-based codebase index.

Builds a compact map of files, classes, functions, and top-level imports.
Injected into the system prompt so the agent knows codebase structure before
exploring. Inspired by Aider's repo-map technique.

Supports Python (.py) and JavaScript/TypeScript (.js, .ts, .jsx, .tsx) via
AST parsing. Other files are listed by path only.
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Directories to skip entirely
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".ruff_cache", ".tox", ".pytest_cache",
    "dist", "build", ".next", ".nuxt", "coverage",
    ".egg-info", "eggs", "*.egg-info",
})

# File extensions to parse for structure
_PYTHON_EXTS = frozenset({".py"})
_JS_EXTS = frozenset({".js", ".ts", ".jsx", ".tsx", ".mjs"})
_PARSEABLE_EXTS = _PYTHON_EXTS | _JS_EXTS

# File extensions to list (but not parse)
_LIST_EXTS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs",
    ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
    ".md", ".rst", ".txt",
    ".html", ".css", ".scss",
    ".sql", ".sh", ".bash",
    ".go", ".rs", ".java", ".kt", ".rb", ".php",
    ".c", ".cpp", ".h", ".hpp",
    ".svelte", ".vue",
})

# Max files to process (safety valve for huge repos)
_MAX_FILES = 500
# Max total output tokens (rough: 1 token ≈ 4 chars)
_MAX_OUTPUT_CHARS = 12000


@dataclass
class FileSymbol:
    """A symbol (class, function, method) found in a file."""
    name: str
    kind: str  # "class", "function", "method", "export"
    line: int
    children: list[FileSymbol] = field(default_factory=list)


@dataclass
class FileEntry:
    """A file's structure in the repo map."""
    path: str
    symbols: list[FileSymbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)


def build_repo_map(workspace: str, max_chars: int = _MAX_OUTPUT_CHARS) -> str | None:
    """Build a compact repo map string for the given workspace.

    Returns None if the workspace is empty or too small to bother mapping.
    The output is designed to fit in ~3000 tokens of system prompt.
    """
    ws = Path(workspace)
    if not ws.is_dir():
        return None

    entries = _collect_files(ws)
    if len(entries) < 2:
        return None

    # Parse structure for supported file types
    for entry in entries:
        ext = Path(entry.path).suffix
        full_path = ws / entry.path
        if ext in _PYTHON_EXTS:
            _parse_python(full_path, entry)
        elif ext in _JS_EXTS:
            _parse_js_simple(full_path, entry)

    # Format the map
    output = _format_map(entries)

    # Truncate if too long
    if len(output) > max_chars:
        output = output[:max_chars] + "\n... (repo map truncated)"

    return output


def _collect_files(ws: Path) -> list[FileEntry]:
    """Walk the workspace and collect relevant files."""
    entries: list[FileEntry] = []

    for root, dirs, files in os.walk(ws):
        # Prune skip directories
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]

        rel_root = Path(root).relative_to(ws)

        for fname in sorted(files):
            if len(entries) >= _MAX_FILES:
                return entries

            ext = Path(fname).suffix
            if ext not in _LIST_EXTS:
                continue

            rel_path = str(rel_root / fname) if str(rel_root) != "." else fname
            entries.append(FileEntry(path=rel_path))

    return entries


def _parse_python(path: Path, entry: FileEntry) -> None:
    """Extract classes, functions, and key imports from a Python file."""
    try:
        source = path.read_text(errors="replace")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            cls = FileSymbol(name=node.name, kind="class", line=node.lineno)
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    cls.children.append(
                        FileSymbol(name=item.name, kind="method", line=item.lineno)
                    )
            entry.symbols.append(cls)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entry.symbols.append(
                FileSymbol(name=node.name, kind="function", line=node.lineno)
            )

        elif isinstance(node, ast.Import):
            for alias in node.names:
                entry.imports.append(alias.name)

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                entry.imports.append(node.module)


# Simple regex-based JS/TS parser (no dependency on tree-sitter)
_JS_CLASS_RE = re.compile(r"^(?:export\s+)?class\s+(\w+)", re.MULTILINE)
_JS_FUNC_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.MULTILINE
)
_JS_ARROW_EXPORT_RE = re.compile(
    r"^export\s+(?:const|let)\s+(\w+)\s*=", re.MULTILINE
)
_JS_IMPORT_RE = re.compile(
    r"^import\s+.*?from\s+['\"]([^'\"]+)['\"]", re.MULTILINE
)


def _parse_js_simple(path: Path, entry: FileEntry) -> None:
    """Extract classes, functions, exports from JS/TS files using regex."""
    try:
        source = path.read_text(errors="replace")
    except OSError:
        return

    lines = source.splitlines()

    for m in _JS_CLASS_RE.finditer(source):
        line_no = source[:m.start()].count("\n") + 1
        entry.symbols.append(FileSymbol(name=m.group(1), kind="class", line=line_no))

    for m in _JS_FUNC_RE.finditer(source):
        line_no = source[:m.start()].count("\n") + 1
        entry.symbols.append(FileSymbol(name=m.group(1), kind="function", line=line_no))

    for m in _JS_ARROW_EXPORT_RE.finditer(source):
        line_no = source[:m.start()].count("\n") + 1
        entry.symbols.append(FileSymbol(name=m.group(1), kind="export", line=line_no))

    for m in _JS_IMPORT_RE.finditer(source):
        entry.imports.append(m.group(1))


# --- Persistence helpers (graph memory integration) ---

_MAP_HEADER_SEPARATOR = "\n---\n"


def format_map_with_commit(map_text: str, commit_sha: str) -> str:
    """Wrap a repo map with the commit SHA it was built from."""
    return f"commit:{commit_sha}{_MAP_HEADER_SEPARATOR}{map_text}"


def parse_stored_map(stored: str) -> tuple[str | None, str]:
    """Parse a stored map into (commit_sha, map_text).

    Returns (None, stored) if no header is found.
    """
    if _MAP_HEADER_SEPARATOR in stored:
        header, map_text = stored.split(_MAP_HEADER_SEPARATOR, 1)
        if header.startswith("commit:"):
            return header[7:].strip(), map_text
    return None, stored


def parse_single_file(workspace: str, relative_path: str) -> FileEntry:
    """Parse a single file and return its FileEntry."""
    entry = FileEntry(path=relative_path)
    full_path = Path(workspace) / relative_path
    if not full_path.is_file():
        return entry
    ext = full_path.suffix
    if ext in _PYTHON_EXTS:
        _parse_python(full_path, entry)
    elif ext in _JS_EXTS:
        _parse_js_simple(full_path, entry)
    return entry


def patch_map(
    existing_map_text: str,
    workspace: str,
    changed_files: list[str],
) -> str:
    """Incrementally update a repo map by re-parsing only changed files.

    Parses the existing map text, replaces entries for changed files
    (or removes them if deleted), and re-formats.
    """
    # Parse existing map into entries
    entries_by_path = _parse_map_text(existing_map_text)

    for file_path in changed_files:
        full_path = Path(workspace) / file_path
        ext = Path(file_path).suffix
        if ext not in _LIST_EXTS:
            continue

        if full_path.is_file():
            # File exists — re-parse it
            new_entry = parse_single_file(workspace, file_path)
            entries_by_path[file_path] = new_entry
        else:
            # File was deleted
            entries_by_path.pop(file_path, None)

    entries = sorted(entries_by_path.values(), key=lambda e: e.path)
    return _format_map(entries)


def _parse_map_text(map_text: str) -> dict[str, FileEntry]:
    """Parse a formatted map string back into FileEntry objects.

    This is a best-effort reverse of _format_map. We extract file paths
    and their symbols from the indented text format.
    """
    entries: dict[str, FileEntry] = {}
    current_entry: FileEntry | None = None

    for line in map_text.splitlines():
        if not line.strip():
            continue

        # File path lines have 2-space indent, no deeper
        if line.startswith("  ") and not line.startswith("    "):
            path = line.strip()
            current_entry = FileEntry(path=path)
            entries[path] = current_entry

        elif line.startswith("    ") and current_entry is not None:
            # Symbol line — we don't need to fully reconstruct symbols
            # since we'll re-parse changed files anyway. Just preserve
            # the entry so unchanged files keep their structure.
            text = line.strip()
            if text.startswith("class "):
                parts = text[6:].split(": ", 1)
                name = parts[0]
                children = []
                if len(parts) > 1:
                    children = [
                        FileSymbol(name=m.strip(), kind="method", line=0)
                        for m in parts[1].split(", ")
                        if m.strip()
                    ]
                current_entry.symbols.append(
                    FileSymbol(name=name, kind="class", line=0, children=children)
                )
            elif text.startswith("function "):
                current_entry.symbols.append(
                    FileSymbol(name=text[9:], kind="function", line=0)
                )
            elif text.startswith("export "):
                current_entry.symbols.append(
                    FileSymbol(name=text[7:], kind="export", line=0)
                )

    return entries


def _format_map(entries: list[FileEntry]) -> str:
    """Format entries into a compact, readable repo map string."""
    parts: list[str] = []

    # Group by top-level directory
    groups: dict[str, list[FileEntry]] = {}
    for entry in entries:
        top = entry.path.split("/")[0] if "/" in entry.path else "."
        groups.setdefault(top, []).append(entry)

    for group_name in sorted(groups):
        group_entries = groups[group_name]

        for entry in group_entries:
            if entry.symbols:
                # File with structure — show symbols
                parts.append(f"  {entry.path}")
                for sym in entry.symbols:
                    if sym.kind == "class":
                        methods = ", ".join(c.name for c in sym.children if c.name != "__init__")
                        if methods:
                            parts.append(f"    class {sym.name}: {methods}")
                        else:
                            parts.append(f"    class {sym.name}")
                    else:
                        parts.append(f"    {sym.kind} {sym.name}")
            else:
                # File without parsed structure — just list it
                parts.append(f"  {entry.path}")

    return "\n".join(parts)
