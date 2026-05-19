"""Pattern set for excluding test/mock/fixture files from graph analysis.

Test files dominate the gap-fill cost (mock data, snapshot helpers, etc.)
yet rarely carry real dispatch edges. The pipeline filters them at the
file walk so they never produce nodes, edges, or checkpoint entries.

Hardcoded for v1. Future per-repo override via .auto-agent/graph.yml is
out of scope.
"""

from __future__ import annotations

import re


_TEST_DIR_NAMES = frozenset(
    {
        "__tests__",
        "__mocks__",
        "tests",
        "test",
        "cypress",
        "e2e",
    }
)

_TEST_FILE_RE = re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|py)$")


def is_test_file(rel_path: str) -> bool:
    """Return True iff ``rel_path`` should be skipped by the graph walk."""
    if _TEST_FILE_RE.search(rel_path):
        return True
    parts = rel_path.split("/")
    return any(p in _TEST_DIR_NAMES for p in parts)
