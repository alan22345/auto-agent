"""Tests for repo map persistence in graph memory."""

from agent.context.repo_map import (
    FileEntry,
    FileSymbol,
    _parse_map_text,
    format_map_with_commit,
    parse_single_file,
    parse_stored_map,
    patch_map,
)


class TestFormatAndParseCommit:
    def test_roundtrip(self):
        map_text = "  app.py\n    function main\n  models.py\n    class User: save"
        sha = "abc123def456"
        stored = format_map_with_commit(map_text, sha)
        parsed_sha, parsed_map = parse_stored_map(stored)
        assert parsed_sha == sha
        assert parsed_map == map_text

    def test_parse_no_header(self):
        raw = "  app.py\n    function main"
        sha, map_text = parse_stored_map(raw)
        assert sha is None
        assert map_text == raw

    def test_parse_with_commit_header(self):
        stored = "commit:abc123\n---\n  app.py\n    function main"
        sha, map_text = parse_stored_map(stored)
        assert sha == "abc123"
        assert map_text == "  app.py\n    function main"


class TestParseMapText:
    def test_parses_file_paths(self):
        map_text = "  app.py\n  models.py\n  utils.py"
        entries = _parse_map_text(map_text)
        assert set(entries.keys()) == {"app.py", "models.py", "utils.py"}

    def test_parses_classes(self):
        map_text = "  models.py\n    class User: save, delete"
        entries = _parse_map_text(map_text)
        assert "models.py" in entries
        symbols = entries["models.py"].symbols
        assert len(symbols) == 1
        assert symbols[0].name == "User"
        assert symbols[0].kind == "class"
        assert len(symbols[0].children) == 2

    def test_parses_functions(self):
        map_text = "  utils.py\n    function slugify\n    function parse_url"
        entries = _parse_map_text(map_text)
        symbols = entries["utils.py"].symbols
        assert len(symbols) == 2
        assert symbols[0].name == "slugify"
        assert symbols[0].kind == "function"

    def test_parses_exports(self):
        map_text = "  config.ts\n    export API_URL"
        entries = _parse_map_text(map_text)
        symbols = entries["config.ts"].symbols
        assert len(symbols) == 1
        assert symbols[0].name == "API_URL"
        assert symbols[0].kind == "export"

    def test_empty_map(self):
        entries = _parse_map_text("")
        assert entries == {}


class TestParseSingleFile:
    def test_parses_python_file(self, tmp_path):
        src = tmp_path / "utils.py"
        src.write_text("def helper():\n    pass\n")
        entry = parse_single_file(str(tmp_path), "utils.py")
        assert entry.path == "utils.py"
        assert len(entry.symbols) == 1
        assert entry.symbols[0].name == "helper"

    def test_returns_empty_for_missing_file(self, tmp_path):
        entry = parse_single_file(str(tmp_path), "nonexistent.py")
        assert entry.path == "nonexistent.py"
        assert entry.symbols == []

    def test_parses_js_file(self, tmp_path):
        src = tmp_path / "app.js"
        src.write_text("function render() { return null; }\n")
        entry = parse_single_file(str(tmp_path), "app.js")
        assert len(entry.symbols) == 1
        assert entry.symbols[0].name == "render"


class TestPatchMap:
    def test_updates_changed_file(self, tmp_path):
        # Start with a map that has an old version of utils.py
        old_map = "  app.py\n    function main\n  utils.py\n    function old_helper"

        # Create a new version of utils.py
        (tmp_path / "utils.py").write_text("def new_helper():\n    pass\n")
        (tmp_path / "app.py").write_text("def main():\n    pass\n")

        result = patch_map(old_map, str(tmp_path), ["utils.py"])
        assert "new_helper" in result
        assert "old_helper" not in result
        assert "main" in result  # unchanged file preserved

    def test_removes_deleted_file(self, tmp_path):
        old_map = "  app.py\n    function main\n  deleted.py\n    function gone"
        (tmp_path / "app.py").write_text("def main():\n    pass\n")
        # deleted.py doesn't exist on disk

        result = patch_map(old_map, str(tmp_path), ["deleted.py"])
        assert "deleted.py" not in result
        assert "main" in result

    def test_adds_new_file(self, tmp_path):
        old_map = "  app.py\n    function main"
        (tmp_path / "app.py").write_text("def main():\n    pass\n")
        (tmp_path / "new_module.py").write_text("class NewThing:\n    def do_it(self): pass\n")

        result = patch_map(old_map, str(tmp_path), ["new_module.py"])
        assert "NewThing" in result
        assert "main" in result

    def test_no_changes_preserves_map(self, tmp_path):
        old_map = "  app.py\n    function main"
        (tmp_path / "app.py").write_text("def main():\n    pass\n")

        result = patch_map(old_map, str(tmp_path), [])
        assert "main" in result

    def test_ignores_non_listable_extensions(self, tmp_path):
        old_map = "  app.py\n    function main"
        (tmp_path / "app.py").write_text("def main():\n    pass\n")
        (tmp_path / "image.png").write_bytes(b"\x89PNG")

        result = patch_map(old_map, str(tmp_path), ["image.png"])
        assert "image.png" not in result
        assert "main" in result
