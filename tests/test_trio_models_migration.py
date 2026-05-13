"""Verifies migration 033 against a live Postgres at HEAD.

Skipped if no Postgres is reachable, matching the pattern in
tests/test_verify_review_models.py.
"""
import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="needs live Postgres",
)


def test_trio_tables_exist():
    engine = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert "architect_attempts" in tables
    assert "trio_review_attempts" in tables


def test_task_trio_columns_exist():
    engine = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("tasks")}
    for c in ("parent_task_id", "trio_phase", "trio_backlog", "consulting_architect"):
        assert c in cols, f"missing column: {c}"


def test_taskstatus_has_trio_values():
    engine = create_engine(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT unnest(enum_range(NULL::taskstatus))").fetchall()
    values = {r[0] for r in rows}
    assert "trio_executing" in values
    assert "trio_review" in values


@pytest.mark.asyncio
async def test_clarification_columns_exist(session: AsyncSession) -> None:
    """Migration 034 adds the clarification + product_brief columns."""
    # Repo.product_brief
    cols = (
        await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'repos' AND column_name = 'product_brief'"
        ))
    ).scalars().all()
    assert cols == ["product_brief"], "Repo.product_brief should exist"

    # ArchitectAttempt clarification columns
    cols = (
        await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'architect_attempts' "
            "AND column_name IN ('clarification_question', "
            "                    'clarification_answer', "
            "                    'clarification_source', "
            "                    'session_blob_path')"
        ))
    ).scalars().all()
    assert set(cols) == {
        "clarification_question", "clarification_answer",
        "clarification_source", "session_blob_path",
    }
