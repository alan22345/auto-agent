"""Tests for the .gitignore helper in eval providers.

Ensures noise files (node_modules, __pycache__, etc.) are excluded from the
git diff so the grader doesn't receive multi-MB token storms.
"""

import os
import subprocess
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "eval", "providers"))

from agent_provider import _GITIGNORE_CONTENT, _write_gitignore


class TestWriteGitignore:
    def test_creates_gitignore(self, tmp_path):
        _write_gitignore(str(tmp_path))
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert "node_modules" in content
        assert "__pycache__" in content
        assert "package-lock.json" in content

    def test_does_not_overwrite_existing(self, tmp_path):
        existing = tmp_path / ".gitignore"
        existing.write_text("my-custom-rule\n")
        _write_gitignore(str(tmp_path))
        # Existing content preserved
        assert existing.read_text() == "my-custom-rule\n"

    def test_gitignore_excludes_node_modules_in_git(self, tmp_path):
        """End-to-end: node_modules should not appear in git status after .gitignore."""
        # Set up workspace with .gitignore
        _write_gitignore(str(tmp_path))

        # Create some legit files and some noise
        (tmp_path / "app.py").write_text("print('hello')\n")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg.js").write_text("module.exports = {}\n")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "app.cpython-312.pyc").write_text("bytecode")
        (tmp_path / "package-lock.json").write_text("{}\n")

        # Init git + add
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)

        # Check staged files
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=tmp_path, capture_output=True, text=True, check=True,
        )
        staged = result.stdout.strip().splitlines()

        # app.py and .gitignore should be staged
        assert "app.py" in staged
        assert ".gitignore" in staged

        # Noise should NOT be staged
        assert not any("node_modules" in line for line in staged)
        assert not any("__pycache__" in line for line in staged)
        assert "package-lock.json" not in staged


class TestGitignoreContent:
    """Verify the .gitignore content covers common noise."""

    REQUIRED_PATTERNS = [
        "node_modules",
        "__pycache__",
        ".venv",
        ".pytest_cache",
        "dist",
        "build",
        "package-lock.json",
        "yarn.lock",
    ]

    def test_has_required_patterns(self):
        for pattern in self.REQUIRED_PATTERNS:
            assert pattern in _GITIGNORE_CONTENT, f"Missing {pattern} in .gitignore"
