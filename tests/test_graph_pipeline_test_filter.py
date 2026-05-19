"""Verifies the test-file exclusion patterns for the ADR-016 graph walk."""

import pytest

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
