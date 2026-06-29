"""Refuse to run the DB-backed suite against a real (non-test) database.

The DB-backed tests seed organizations + tasks and create/drop tables. Pointed
at production they silently fill the live DB with junk — exactly the 2026-06-27
incident, where repo 170's health-loop coder (prompted to "run the repo's tests
before finishing") ran pytest inside the prod container with DATABASE_URL=prod
and conftest had no guard, spawning ~850 throwaway orgs/tasks that the
orchestrator then dispatched and stranded.

The contract: if DATABASE_URL is set, its database name must end in ``_test``
(or ``ALLOW_NONTEST_DB=1`` must be set to consciously override). An unset
DATABASE_URL is fine — the DB tests skip themselves.
"""

from __future__ import annotations

from urllib.parse import urlsplit


class NonTestDatabaseError(RuntimeError):
    """Raised when the suite is pointed at a database that isn't a test DB."""


def database_name(url: str) -> str:
    """The database name from a SQLAlchemy/asyncpg URL (path after the last '/')."""
    return urlsplit(url).path.lstrip("/").split("?")[0]


def is_test_database(url: str) -> bool:
    """True only if the URL's database name marks it as a throwaway test DB."""
    return database_name(url).endswith("_test")


def assert_test_database(url: str | None, *, allow_override: bool) -> None:
    """Raise unless *url* is safe to run the destructive DB suite against.

    No-op when *url* is empty (DB tests skip) or *allow_override* is set.
    """
    if not url or allow_override:
        return
    if not is_test_database(url):
        raise NonTestDatabaseError(
            f"DATABASE_URL points at non-test database {database_name(url)!r}. "
            "The DB-backed suite creates and drops rows — refusing to run against a "
            "real database. Use a database whose name ends in '_test', or set "
            "ALLOW_NONTEST_DB=1 to override deliberately."
        )
