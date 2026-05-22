"""Round-trip alembic upgrade 053 / downgrade 052 against real Postgres.

Adds nullable flow_json JSONB column on repo_graphs. Phase 1 of the
capability/flow map spec persists the result of forward-tracing flows
from entry points to terminal side effects.
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


def _sync_url() -> str:
    url = os.environ["DATABASE_URL"]
    return url.replace("+asyncpg", "")


def _alembic_cfg() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", _sync_url())
    return cfg


def test_upgrade_053_adds_nullable_flow_json_column() -> None:
    _skip_if_no_db()
    cfg = _alembic_cfg()
    command.upgrade(cfg, "053")
    engine = sa.create_engine(_sync_url())
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name='repo_graphs' AND column_name='flow_json'",
            ),
        ).first()
    assert row is not None, "flow_json column missing — migration 053 not applied"
    data_type, is_nullable = row
    assert data_type == "jsonb"
    assert is_nullable == "YES"


def test_downgrade_053_removes_flow_json_column() -> None:
    _skip_if_no_db()
    cfg = _alembic_cfg()
    command.upgrade(cfg, "053")
    command.downgrade(cfg, "052")
    engine = sa.create_engine(_sync_url())
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='repo_graphs' AND column_name='flow_json'",
            ),
        ).first()
    assert row is None
    # Re-upgrade so subsequent tests in the same Postgres instance get
    # the column back. (Pattern matches test_repo_graph_migration_048.)
    command.upgrade(cfg, "053")


def test_orm_has_nullable_flow_json_column() -> None:
    """Sanity check that the ORM column matches the migration shape.

    Runs without a DB; pure SQLAlchemy metadata introspection.
    """
    from shared.models.core import RepoGraph
    col = RepoGraph.__table__.columns["flow_json"]
    assert col.nullable is True
