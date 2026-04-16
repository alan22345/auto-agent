"""Tests for agent/context/repo_map.py — AST-based codebase indexing."""

import os
import tempfile

from agent.context.repo_map import (
    FileEntry,
    _format_map,
    _parse_js_simple,
    _parse_python,
    build_repo_map,
)


class TestParsePython:
    def test_extracts_classes_and_methods(self, tmp_path):
        src = tmp_path / "models.py"
        src.write_text(
            "class User:\n"
            "    def __init__(self, name): self.name = name\n"
            "    def greet(self): return f'hi {self.name}'\n"
            "\n"
            "class Admin(User):\n"
            "    def ban(self, user): pass\n"
        )
        entry = FileEntry(path="models.py")
        _parse_python(src, entry)

        assert len(entry.symbols) == 2
        assert entry.symbols[0].name == "User"
        assert entry.symbols[0].kind == "class"
        methods = [c.name for c in entry.symbols[0].children]
        assert "__init__" in methods
        assert "greet" in methods
        assert entry.symbols[1].name == "Admin"

    def test_extracts_functions(self, tmp_path):
        src = tmp_path / "utils.py"
        src.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "\n"
            "def slugify(text: str) -> str:\n"
            "    return text.lower().replace(' ', '-')\n"
            "\n"
            "async def fetch_data(url):\n"
            "    pass\n"
        )
        entry = FileEntry(path="utils.py")
        _parse_python(src, entry)

        func_names = [s.name for s in entry.symbols]
        assert "slugify" in func_names
        assert "fetch_data" in func_names
        assert "os" in entry.imports
        assert "pathlib" in entry.imports

    def test_handles_syntax_error_gracefully(self, tmp_path):
        src = tmp_path / "broken.py"
        src.write_text("def foo(\n")  # Invalid syntax
        entry = FileEntry(path="broken.py")
        _parse_python(src, entry)
        assert entry.symbols == []

    def test_handles_empty_file(self, tmp_path):
        src = tmp_path / "empty.py"
        src.write_text("")
        entry = FileEntry(path="empty.py")
        _parse_python(src, entry)
        assert entry.symbols == []


class TestParseJsSimple:
    def test_extracts_classes_and_functions(self, tmp_path):
        src = tmp_path / "app.js"
        src.write_text(
            "import React from 'react';\n"
            "import { useState } from 'react';\n"
            "\n"
            "class App extends React.Component {\n"
            "  render() { return null; }\n"
            "}\n"
            "\n"
            "function helper() { return 1; }\n"
            "\n"
            "export const API_URL = 'http://example.com';\n"
        )
        entry = FileEntry(path="app.js")
        _parse_js_simple(src, entry)

        names = [s.name for s in entry.symbols]
        assert "App" in names
        assert "helper" in names
        assert "API_URL" in names
        assert "react" in entry.imports

    def test_handles_export_class(self, tmp_path):
        src = tmp_path / "model.ts"
        src.write_text("export class UserModel {\n  id: number;\n}\n")
        entry = FileEntry(path="model.ts")
        _parse_js_simple(src, entry)
        assert entry.symbols[0].name == "UserModel"
        assert entry.symbols[0].kind == "class"

    def test_handles_async_function(self, tmp_path):
        src = tmp_path / "api.ts"
        src.write_text("export async function fetchUsers() {\n  return [];\n}\n")
        entry = FileEntry(path="api.ts")
        _parse_js_simple(src, entry)
        assert entry.symbols[0].name == "fetchUsers"


class TestBuildRepoMap:
    def test_builds_map_for_python_project(self, tmp_path):
        # Create a small project structure
        (tmp_path / "app.py").write_text(
            "from models import User\n\ndef main():\n    pass\n"
        )
        (tmp_path / "models.py").write_text(
            "class User:\n    def save(self): pass\n"
        )
        (tmp_path / "README.md").write_text("# My Project\n")

        result = build_repo_map(str(tmp_path))
        assert result is not None
        assert "app.py" in result
        assert "models.py" in result
        assert "class User" in result
        assert "function main" in result

    def test_returns_none_for_empty_dir(self, tmp_path):
        result = build_repo_map(str(tmp_path))
        assert result is None

    def test_returns_none_for_single_file(self, tmp_path):
        (tmp_path / "only.py").write_text("x = 1\n")
        result = build_repo_map(str(tmp_path))
        assert result is None  # Less than 2 files

    def test_skips_git_and_venv(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "lib.py").write_text("x = 1\n")
        (tmp_path / "app.py").write_text("def run(): pass\n")
        (tmp_path / "config.py").write_text("X = 1\n")

        result = build_repo_map(str(tmp_path))
        assert result is not None
        assert ".git" not in result
        assert ".venv" not in result

    def test_truncates_long_output(self, tmp_path):
        # Create many files to exceed max chars
        for i in range(100):
            (tmp_path / f"module_{i:03d}.py").write_text(
                f"class LongClassName{i}:\n"
                f"    def method_a(self): pass\n"
                f"    def method_b(self): pass\n"
                f"    def method_c(self): pass\n"
            )
        result = build_repo_map(str(tmp_path), max_chars=2000)
        assert result is not None
        assert "truncated" in result
        assert len(result) <= 2100  # max_chars + truncation message


class TestFormatMap:
    def test_formats_symbols(self):
        entries = [
            FileEntry(path="app.py", symbols=[]),
            FileEntry(
                path="models.py",
                symbols=[
                    __import__("agent.context.repo_map", fromlist=["FileSymbol"]).FileSymbol(
                        name="User", kind="class", line=1,
                        children=[
                            __import__("agent.context.repo_map", fromlist=["FileSymbol"]).FileSymbol(
                                name="save", kind="method", line=2,
                            ),
                        ],
                    ),
                ],
            ),
        ]
        output = _format_map(entries)
        assert "app.py" in output
        assert "class User: save" in output
