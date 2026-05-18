"""Round-trip alembic upgrade 033 / downgrade 032 against a real Postgres.

Skips when DATABASE_URL is unset so the rest of the suite stays runnable
on a laptop without a local DB.
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def _skip_if_no_db() -> None:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("needs a running Postgres (set DATABASE_URL)")


def _alembic_cfg() -> tuple[Config, str]:
    cfg = Config("alembic.ini")
    raw = os.environ["DATABASE_URL"]
    if "+asyncpg" in raw:
        sync_url = raw.replace("+asyncpg", "+psycopg2")
    elif raw.startswith("postgresql://"):
        sync_url = raw.replace("postgresql://", "postgresql+psycopg2://", 1)
    else:
        sync_url = raw
    cfg.set_main_option("sqlalchemy.url", sync_url)
    return cfg, sync_url


def test_upgrade_to_033_creates_both_tables() -> None:
    _skip_if_no_db()
    cfg, sync_url = _alembic_cfg()
    command.upgrade(cfg, "033")

    engine = sa.create_engine(sync_url)
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT to_regclass('public.repo_graph_configs'), to_regclass('public.repo_graphs')"
            )
        ).fetchone()
        assert rows is not None
        assert rows[0] is not None
        assert rows[1] is not None


def test_downgrade_from_033_to_032_drops_both_tables() -> None:
    _skip_if_no_db()
    cfg, sync_url = _alembic_cfg()
    command.upgrade(cfg, "033")
    try:
        command.downgrade(cfg, "032")

        engine = sa.create_engine(sync_url)
        with engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT to_regclass('public.repo_graph_configs'),"
                    " to_regclass('public.repo_graphs')"
                )
            ).fetchone()
            assert row is not None
            assert row[0] is None
            assert row[1] is None
    finally:
        # Leave the schema in the canonical upgraded state for other tests.
        command.upgrade(cfg, "033")
