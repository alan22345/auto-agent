"""Tests for the integration-branch-name helper — ADR-015 Phase 7.7.

The helper turns a task's (id, title) into a kebab-case integration branch
name of the form ``auto-agent/<slug>-<task_id>``. The slug is derived from
the title by lowercasing, replacing non-alphanumeric runs with ``-``,
stripping leading/trailing ``-``, and truncating to 50 characters.

Backwards compatibility for in-flight tasks that already have ``trio/<id>``
locally + remotely is handled separately by ``resolve_integration_branch``
reading the ``Task.integration_branch`` column with a ``trio/<id>``
fallback when the column is NULL. The helper itself is pure and applies
to NEW tasks only.
"""

from __future__ import annotations

import pytest

from agent.lifecycle.trio.branch_name import integration_branch_name


@pytest.mark.parametrize(
    "task_id, title, expected",
    [
        (1, "Parallel Universe screen", "auto-agent/parallel-universe-screen-1"),
        (42, "Add OAuth login", "auto-agent/add-oauth-login-42"),
        (7, "fix: bug #123", "auto-agent/fix-bug-123-7"),
        # Multiple spaces / mixed-case collapse to a single dash and lowercase.
        (3, "  Hello  World  ", "auto-agent/hello-world-3"),
        # Underscores and dots become dashes.
        (5, "foo_bar.baz", "auto-agent/foo-bar-baz-5"),
    ],
)
def test_slugifies_titles(task_id: int, title: str, expected: str) -> None:
    assert integration_branch_name(task_id, title) == expected


def test_empty_title_falls_back_to_task_default() -> None:
    assert integration_branch_name(42, "") == "auto-agent/task-42"


def test_none_title_falls_back_to_task_default() -> None:
    assert integration_branch_name(42, None) == "auto-agent/task-42"


def test_whitespace_only_title_falls_back() -> None:
    assert integration_branch_name(9, "   \t\n  ") == "auto-agent/task-9"


def test_all_punctuation_title_falls_back() -> None:
    # No alphanumeric characters survive slugification → fall back.
    assert integration_branch_name(11, "!@#$%^&*()") == "auto-agent/task-11"


def test_long_title_truncated_to_50_chars() -> None:
    # 70-char title; slug truncates to 50 chars before joining with id.
    long_title = "a" * 70
    out = integration_branch_name(7, long_title)
    # Branch: auto-agent/<50 chars of slug>-7
    assert out == "auto-agent/" + ("a" * 50) + "-7"
    # Slug body itself is exactly 50 chars (no trailing dash artefact).
    slug_body = out.removeprefix("auto-agent/").removesuffix("-7")
    assert len(slug_body) == 50


def test_long_title_with_trailing_dash_after_truncation_is_stripped() -> None:
    # A title whose 51st character is a delimiter would leave a trailing dash
    # after a naive truncation. The helper must strip it.
    title = ("a" * 49) + " " + ("b" * 30)
    out = integration_branch_name(3, title)
    slug_body = out.removeprefix("auto-agent/").removesuffix("-3")
    # No trailing dash, and we stayed within the 50-char budget.
    assert not slug_body.endswith("-")
    assert len(slug_body) <= 50


def test_unicode_title_is_normalised_or_falls_back() -> None:
    # Non-ASCII characters drop out under the [^a-z0-9]+ rule. A title that
    # is entirely non-ASCII becomes empty → falls back.
    assert integration_branch_name(8, "日本語タイトル") == "auto-agent/task-8"
    # A mixed title keeps the ASCII portion.
    assert integration_branch_name(4, "Hello 日本 World") == "auto-agent/hello-world-4"


def test_leading_and_trailing_dashes_stripped() -> None:
    # A title that starts and ends with punctuation must not leak dashes
    # into the branch name.
    out = integration_branch_name(2, "--leading and trailing--")
    assert out == "auto-agent/leading-and-trailing-2"


def test_numbers_in_title_preserved() -> None:
    out = integration_branch_name(99, "Top 10 features for v2")
    assert out == "auto-agent/top-10-features-for-v2-99"


def test_idempotent_for_same_inputs() -> None:
    a = integration_branch_name(5, "Same title")
    b = integration_branch_name(5, "Same title")
    assert a == b
