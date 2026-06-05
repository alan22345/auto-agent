"""Tests that walk_files and _discover_areas honour _DEFAULT_EXCLUDE_DIRS.

Specifically verifies that:
- .claude/worktrees/ (Claude Code harness worktrees) are never walked
- target/, out/ (build dirs) are never walked
- node_modules/ (already excluded — sanity check) is never walked
- Real source files under agent/ are still included
"""

from __future__ import annotations

import os
from pathlib import Path

from agent.graph_analyzer.pipeline import _discover_areas, walk_files

# Sentinel used to verify Path is a runtime dependency (not annotation-only).
_HERE = Path(__file__).parent


def _build_fixture(tmp_path: Path) -> Path:
    """Create a small directory tree in tmp_path and return its root."""
    # Real source file — must be included
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent" / "real.py").write_text("def main(): pass\n")

    # Worktree copy under .claude — must be excluded
    (tmp_path / ".claude" / "worktrees" / "wt1" / "agent").mkdir(parents=True)
    (tmp_path / ".claude" / "worktrees" / "wt1" / "agent" / "copy.py").write_text(
        "def main(): pass\n"
    )

    # node_modules — already excluded (sanity check)
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("module.exports = {};\n")

    # target/ — build dir — must be excluded
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "gen.py").write_text("# generated\n")

    # out/ — build dir — must be excluded
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "gen.py").write_text("# generated\n")

    return tmp_path


class TestWalkFilesExcludes:
    def test_real_source_file_is_included(self, tmp_path: Path) -> None:
        root = _build_fixture(tmp_path)
        files = walk_files(str(root))
        assert "agent/real.py" in files, f"agent/real.py should be included; got: {files}"

    def test_claude_worktree_files_excluded(self, tmp_path: Path) -> None:
        root = _build_fixture(tmp_path)
        files = walk_files(str(root))
        claude_files = [
            f for f in files if f.startswith(".claude" + os.sep) or f.startswith(".claude/")
        ]
        assert claude_files == [], f".claude/ files should be excluded; found: {claude_files}"

    def test_node_modules_excluded(self, tmp_path: Path) -> None:
        root = _build_fixture(tmp_path)
        files = walk_files(str(root))
        nm_files = [f for f in files if f.startswith("node_modules/")]
        assert nm_files == [], f"node_modules/ files should be excluded; found: {nm_files}"

    def test_target_dir_excluded(self, tmp_path: Path) -> None:
        root = _build_fixture(tmp_path)
        files = walk_files(str(root))
        target_files = [f for f in files if f.startswith("target/")]
        assert target_files == [], f"target/ files should be excluded; found: {target_files}"

    def test_out_dir_excluded(self, tmp_path: Path) -> None:
        root = _build_fixture(tmp_path)
        files = walk_files(str(root))
        out_files = [f for f in files if f.startswith("out/")]
        assert out_files == [], f"out/ files should be excluded; found: {out_files}"

    def test_only_agent_real_py_returned(self, tmp_path: Path) -> None:
        """Aggregate: the full walk result should be exactly [agent/real.py]."""
        root = _build_fixture(tmp_path)
        files = walk_files(str(root))
        assert files == ["agent/real.py"], f"Expected only agent/real.py; got: {files}"


class TestDiscoverAreasExcludes:
    def test_claude_area_not_discovered(self, tmp_path: Path) -> None:
        root = _build_fixture(tmp_path)
        areas = _discover_areas(str(root))
        area_names = [name for name, _ in areas]
        assert ".claude" not in area_names, (
            f".claude should not appear as a discovered area; got: {area_names}"
        )

    def test_target_area_not_discovered(self, tmp_path: Path) -> None:
        root = _build_fixture(tmp_path)
        areas = _discover_areas(str(root))
        area_names = [name for name, _ in areas]
        assert "target" not in area_names, (
            f"target should not appear as a discovered area; got: {area_names}"
        )

    def test_out_area_not_discovered(self, tmp_path: Path) -> None:
        root = _build_fixture(tmp_path)
        areas = _discover_areas(str(root))
        area_names = [name for name, _ in areas]
        assert "out" not in area_names, (
            f"out should not appear as a discovered area; got: {area_names}"
        )

    def test_agent_area_is_discovered(self, tmp_path: Path) -> None:
        root = _build_fixture(tmp_path)
        areas = _discover_areas(str(root))
        area_names = [name for name, _ in areas]
        assert "agent" in area_names, f"agent should be discovered; got: {area_names}"
