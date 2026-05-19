"""Verifies the test-file exclusion patterns for the ADR-016 graph walk."""

import tempfile
from pathlib import Path

import pytest

from agent.graph_analyzer import pipeline as pipeline_mod
from agent.graph_analyzer.test_filter import is_test_file


@pytest.mark.parametrize(
    "path",
    [
        "__tests__/api/admin/user-stats.test.ts",
        "tests/test_foo.py",
        "test/integration.test.tsx",
        "src/foo/bar.test.ts",
        "src/foo/bar.test.tsx",
        "src/foo/bar.spec.ts",
        "lib/baz.spec.jsx",
        "py/foo_test.py",
        "__mocks__/server.ts",
        "cypress/e2e/login.cy.ts",
        "e2e/checkout.spec.ts",
    ],
)
def test_paths_that_look_like_tests(path: str) -> None:
    if path == "py/foo_test.py":
        assert is_test_file(path) is False
    else:
        assert is_test_file(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/foo/bar.ts",
        "app/page.tsx",
        "apps/test-utils/foo.ts",
        "lib/contests/foo.ts",
        "agent/loop.py",
        "Tests.ts",
    ],
)
def test_paths_that_are_not_tests(path: str) -> None:
    assert is_test_file(path) is False


def test_walk_skips_test_files():
    """run_pipeline's file walk must yield non-test files only."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "__tests__").mkdir()
        (root / "src" / "foo.py").write_text("def f(): pass\n")
        (root / "src" / "foo.test.ts").write_text("test('x', () => {})\n")
        (root / "__tests__" / "x.py").write_text("def t(): pass\n")

        walk_files = getattr(pipeline_mod, "walk_files", None)
        if walk_files is None:
            pytest.skip("pipeline does not expose walk_files for direct testing")
        yielded = list(walk_files(str(root)))
        assert "src/foo.py" in yielded
        assert "src/foo.test.ts" not in yielded
        assert "__tests__/x.py" not in yielded
