"""Tests for eval/providers/agent_provider.py — output size capping and noise filtering.

These test the file-filtering and size-capping logic that prevents the eval
grader from receiving multi-MB payloads (caused by node_modules, etc).
"""

import json
import os
import sys

# Make eval/providers importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "eval", "providers"))


class TestPathFiltering:
    """Unit-test the path filtering logic by replicating the filter inline."""

    # Keep this in sync with agent_provider.py
    _SKIP_PATH_PARTS = frozenset({
        "node_modules", "__pycache__", ".venv", "venv",
        ".mypy_cache", ".ruff_cache", ".pytest_cache", ".tox",
        "dist", "build", ".next", ".nuxt", "coverage",
        ".git",
    })

    def _filter(self, paths: list[str]) -> set[str]:
        """Replicate the filter logic from agent_provider.py."""
        result = set()
        for path in paths:
            parts = path.split("/")
            if any(p in self._SKIP_PATH_PARTS for p in parts):
                continue
            result.add(path)
        return result

    def test_keeps_regular_files(self):
        paths = ["app.py", "src/main.py", "tests/test_app.py"]
        assert self._filter(paths) == set(paths)

    def test_drops_node_modules(self):
        paths = [
            "app.js",
            "node_modules/foo/index.js",
            "node_modules/@scope/pkg/dist/main.js",
        ]
        result = self._filter(paths)
        assert result == {"app.js"}

    def test_drops_pycache(self):
        paths = ["app.py", "__pycache__/app.cpython-312.pyc", "src/__pycache__/x.pyc"]
        result = self._filter(paths)
        assert result == {"app.py"}

    def test_drops_venv(self):
        paths = ["app.py", ".venv/lib/python3.12/site-packages/foo.py", "venv/bin/python"]
        assert self._filter(paths) == {"app.py"}

    def test_drops_build_artifacts(self):
        paths = ["src/app.py", "dist/bundle.js", "build/lib/main.py", ".next/static/chunk.js"]
        assert self._filter(paths) == {"src/app.py"}

    def test_nested_skip_dir_filters_everything_below(self):
        paths = [
            "src/app.py",
            "src/node_modules/foo.js",  # node_modules nested anywhere drops it
        ]
        assert self._filter(paths) == {"src/app.py"}


class TestOutputSizeCapping:
    """Replicate the output size cap logic and verify it works."""

    _MAX_OUTPUT_BYTES = 500_000

    def _cap(self, output: dict) -> dict:
        """Replicate the cap logic from agent_provider.py."""
        serialized = json.dumps(output)
        if len(serialized) > self._MAX_OUTPUT_BYTES:
            while output["files"] and len(json.dumps(output)) > self._MAX_OUTPUT_BYTES:
                largest = max(output["files"], key=lambda k: len(output["files"][k]))
                del output["files"][largest]
            if len(json.dumps(output)) > self._MAX_OUTPUT_BYTES:
                output["diff"] = output["diff"][:2000]
            output["_truncated"] = True
        return output

    def test_small_output_not_capped(self):
        output = {
            "agent_output": "done",
            "files": {"app.py": "x = 1\n"},
            "diff": "+x = 1",
        }
        result = self._cap(output)
        assert "_truncated" not in result
        assert len(result["files"]) == 1

    def test_large_files_dropped(self):
        # Create an output > 500KB
        big_content = "x" * 100_000  # 100KB per file
        output = {
            "agent_output": "done",
            "files": {f"file_{i}.py": big_content for i in range(10)},  # 1MB total
            "diff": "small diff",
        }
        result = self._cap(output)
        assert result.get("_truncated") is True
        assert len(json.dumps(result)) <= self._MAX_OUTPUT_BYTES + 100

    def test_at_least_some_files_remain_if_possible(self):
        # Output just over limit — should drop at least some but keep the small ones
        output = {
            "agent_output": "",
            "files": {
                "small.py": "a",
                "big.py": "x" * 600_000,
            },
            "diff": "",
        }
        result = self._cap(output)
        # The big file should be dropped
        assert "big.py" not in result["files"]
        assert "small.py" in result["files"]

    def test_diff_truncated_if_still_over(self):
        # Everything huge — diff should get truncated too
        output = {
            "agent_output": "",
            "files": {},  # Empty
            "diff": "x" * 600_000,  # Only diff is huge
        }
        result = self._cap(output)
        assert len(result["diff"]) <= 2000
