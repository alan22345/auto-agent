"""Round-trip alembic upgrade 029 / downgrade 028 against a real Postgres.

Skip-if no DATABASE_URL so local mock-based tests still pass standalone.

Note: pytestmark.skipif is evaluated at *collection* time, before any test in
other modules runs.  However, test_eval_gitignore.py imports agent_provider
which calls load_dotenv() as a side-effect, injecting DATABASE_URL into
os.environ *at test-execution time* (not collection time).  Any test that runs
after test_eval_gitignore.py in the same session would therefore see DATABASE_URL
set even though it was absent at collection.  We guard with an explicit
pytest.skip() inside each test function (evaluated at run time) to handle this.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def _skip_if_no_db() -> None:
    """Skip the calling test if DATABASE_URL is not set at run time."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("needs a running Postgres (set DATABASE_URL)")


def _sync_url() -> str:
    """Return the sync (psycopg2) URL derived from DATABASE_URL."""
    return os.environ["DATABASE_URL"].replace("+asyncpg", "").replace(
        "postgresql://", "postgresql+psycopg2://"
    ).replace("postgresql+psycopg2://", "postgresql+psycopg2://")  # idempotent


def _alembic_cfg() -> tuple[Config, str]:
    """Return (alembic Config, sync_url). Store the URL before alembic may mutate cfg."""
    cfg = Config("alembic.ini")
    # Alembic uses sync drivers; strip async dialect marker.
    raw = os.environ["DATABASE_URL"]
    # Handle postgresql+asyncpg:// → postgresql+psycopg2://
    if "+asyncpg" in raw:
        sync_url = raw.replace("+asyncpg", "+psycopg2")
    elif raw.startswith("postgresql://"):
        sync_url = raw.replace("postgresql://", "postgresql+psycopg2://", 1)
    else:
        sync_url = raw
    cfg.set_main_option("sqlalchemy.url", sync_url)
    return cfg, sync_url


def test_upgrade_to_029_creates_plans_and_seeds_three_rows() -> None:
    _skip_if_no_db()
    cfg, sync_url = _alembic_cfg()
    command.upgrade(cfg, "029")

    engine = sa.create_engine(sync_url)
    with engine.begin() as conn:
        rows = conn.execute(sa.text("SELECT name FROM plans ORDER BY id")).fetchall()
        assert [r[0] for r in rows] == ["free", "pro", "team"]
        null_plans = conn.execute(
            sa.text("SELECT COUNT(*) FROM organizations WHERE plan_id IS NULL")
        ).scalar_one()
        assert null_plans == 0


def test_downgrade_from_029_to_028_drops_plans_and_column() -> None:
    _skip_if_no_db()
    cfg, sync_url = _alembic_cfg()
    command.upgrade(cfg, "029")
    try:
        command.downgrade(cfg, "028")

        engine = sa.create_engine(sync_url)
        # Use separate connections for the two checks: the ProgrammingError on the
        # first query aborts the psycopg2 transaction, making the second query fail
        # with InFailedSqlTransaction if they share a connection.
        with engine.begin() as conn, pytest.raises(sa.exc.ProgrammingError):
            conn.execute(sa.text("SELECT 1 FROM plans")).fetchone()
        with engine.begin() as conn:
            cols = conn.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='organizations'"
                )
            ).fetchall()
            assert "plan_id" not in {c[0] for c in cols}
    finally:
        # Restore the DB to 029 so subsequent tests see the Phase 4 schema.
        command.upgrade(cfg, "029")
