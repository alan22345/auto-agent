"""Round-trip alembic upgrade 048 / downgrade 047 against real Postgres."""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config


def _skip_if_no_db() -> None:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("needs a running Postgres (set DATABASE_URL)")


def _sync_url() -> str:
    url = os.environ["DATABASE_URL"]
    return url.replace("+asyncpg", "")


def _alembic_cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _sync_url())
    return cfg


def test_upgrade_adds_checkpoint_columns() -> None:
    _skip_if_no_db()
    cfg = _alembic_cfg()
    command.upgrade(cfg, "048")
    engine = sa.create_engine(_sync_url())
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name='repo_graphs' AND column_name IN "
                "('is_complete','processed_files','failed_sites')"
            )
        ).all()
    assert {r[0] for r in rows} == {"is_complete", "processed_files", "failed_sites"}


def test_existing_rows_marked_complete() -> None:
    _skip_if_no_db()
    cfg = _alembic_cfg()
    command.upgrade(cfg, "048")
    engine = sa.create_engine(_sync_url())
    with engine.connect() as conn:
        out = conn.execute(
            sa.text("SELECT bool_and(is_complete) FROM repo_graphs")
        ).scalar()
    assert out is None or out is True


def test_downgrade_drops_columns() -> None:
    _skip_if_no_db()
    cfg = _alembic_cfg()
    command.upgrade(cfg, "048")
    command.downgrade(cfg, "047")
    engine = sa.create_engine(_sync_url())
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='repo_graphs' AND column_name IN "
                "('is_complete','processed_files','failed_sites')"
            )
        ).all()
    assert rows == []
    command.upgrade(cfg, "048")
