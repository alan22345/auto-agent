"""The DB-suite safety guard: never run destructive tests against a real database.

Regression lock for the 2026-06-27 incident where the health-loop coder ran the
DB-backed suite with DATABASE_URL=prod and seeded ~850 junk orgs/tasks into
production.
"""

from __future__ import annotations

import pytest

from tests.db_guard import (
    NonTestDatabaseError,
    assert_test_database,
    database_name,
    is_test_database,
)

PROD = "postgresql+asyncpg://autoagent:pw@harpoon-prod-db.eu-central-1.rds.amazonaws.com:5432/autoagent"
TEST = "postgresql+asyncpg://alan@localhost:5432/autoagent_test"


@pytest.mark.parametrize(
    "url,expected",
    [
        (TEST, True),
        (TEST + "?ssl=require", True),
        ("postgresql+asyncpg://u:p@h:5432/foo_test", True),
        (PROD, False),
        ("postgresql+asyncpg://u:p@h:5432/autoagent", False),
        ("postgresql+asyncpg://u:p@h:5432/postgres", False),
    ],
)
def test_is_test_database(url, expected) -> None:
    assert is_test_database(url) is expected


def test_database_name_strips_query() -> None:
    assert database_name(TEST + "?ssl=require") == "autoagent_test"
    assert database_name(PROD) == "autoagent"


def test_assert_blocks_prod() -> None:
    with pytest.raises(NonTestDatabaseError, match="non-test database"):
        assert_test_database(PROD, allow_override=False)


def test_assert_allows_test_db() -> None:
    assert_test_database(TEST, allow_override=False)  # must not raise


def test_override_bypasses_guard() -> None:
    assert_test_database(PROD, allow_override=True)  # deliberate override, no raise


def test_unset_url_is_noop() -> None:
    assert_test_database(None, allow_override=False)  # DB tests skip anyway
    assert_test_database("", allow_override=False)
