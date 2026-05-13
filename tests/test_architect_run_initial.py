"""Tests for ``architect.run_initial`` — the first architect pass.

The two seams that talk to the outside world (workspace prep and PR
plumbing) are patched; the real DB session is used so we exercise the
``architect_attempts`` row, ``Task.trio_backlog`` and ``Task.status``
writes against the actual schema.

``architect.async_session`` is patched to a factory that yields the
test's transaction-wrapped session so every write the implementation
makes lands inside the per-test savepoint and rolls back cleanly.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import inspect, select

from agent.lifecycle.trio import architect
from shared.models import (
    ArchitectAttempt,
    ArchitectPhase,
    Organization,
    Plan,
    Task,
    TaskComplexity,
    TaskSource,
    TaskStatus,
    TrioPhase,
)


async def _skip_if_trio_columns_missing(session) -> None:
    """Skip the test if the connected DB hasn't run the trio migration.

    The conftest ``session`` fixture only skips when ``DATABASE_URL`` is
    unset; collection-time skipping doesn't help when another test
    (``test_eval_gitignore.py`` and friends) load ``.env`` mid-suite and
    inject the URL into ``os.environ``. In that case INSERTs would crash
    against a real DB that lacks ``tasks.parent_task_id``. Skip cleanly.
    """
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set")

    def _trio_cols(sync_conn) -> set[str]:
        insp = inspect(sync_conn)
        cols = {c["name"] for c in insp.get_columns("tasks")}
        return cols & {"parent_task_id", "trio_phase", "trio_backlog"}

    conn = await session.connection()
    present = await conn.run_sync(_trio_cols)
    if len(present) < 3:
        pytest.skip(
            "trio columns not present in DATABASE_URL "
            "— run `alembic upgrade head`",
        )


async def _seed_org(session) -> Organization:
    plan = Plan(
        name=f"plan-{uuid.uuid4().hex[:6]}",
        max_concurrent_tasks=2,
        max_tasks_per_day=100,
        max_input_tokens_per_day=10_000_000,
        max_output_tokens_per_day=2_500_000,
        max_members=5,
    )
    session.add(plan)
    await session.flush()
    org = Organization(
        name=f"org-{uuid.uuid4().hex[:6]}",
        slug=f"org-{uuid.uuid4().hex[:8]}",
        plan_id=plan.id,
    )
    session.add(org)
    await session.flush()
    return org


async def _seed_parent(session) -> Task:
    org = await _seed_org(session)
    t = Task(
        title="Build a TODO app",
        description="Build a TODO app",
        source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING,
        complexity=TaskComplexity.COMPLEX_LARGE,
        trio_phase=TrioPhase.ARCHITECTING,
        organization_id=org.id,
    )
    session.add(t)
    await session.flush()
    return t


def _patched_async_session(real_session):
    """Build a factory that yields ``real_session`` from ``async with``.

    ``architect.run_initial`` opens sessions via
    ``async with async_session() as s: ...; await s.commit()``. We swap
    that callable so every block re-enters the same savepoint-protected
    test session — and we transparently swallow ``commit()``/``close()``
    so the test fixture's transaction can roll the whole run back.
    """
    # Wrap commit/close so the per-block lifecycle the implementation
    # expects doesn't tear down the test transaction.
    real_session.commit = AsyncMock(side_effect=lambda: real_session.flush())
    real_session.close = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


def test_extract_backlog_happy_path():
    text = (
        "Some reasoning.\n\n"
        "```json\n"
        '{"backlog": [{"id": "a", "title": "T1", "description": "D1"}]}\n'
        "```"
    )
    out = architect._extract_backlog(text)
    assert out is not None
    assert out[0]["id"] == "a"
    # Default status is filled in.
    assert out[0]["status"] == "pending"


def test_extract_backlog_no_block_returns_none():
    assert architect._extract_backlog("just prose, no JSON") is None
    assert architect._extract_backlog("") is None
    assert architect._extract_backlog(None) is None  # type: ignore[arg-type]


def test_extract_backlog_malformed_json_returns_none():
    text = "```json\n{not valid json}\n```"
    assert architect._extract_backlog(text) is None


def test_extract_backlog_missing_backlog_key_returns_none():
    text = '```json\n{"other": []}\n```'
    assert architect._extract_backlog(text) is None


def test_extract_backlog_empty_list_returns_none():
    text = '```json\n{"backlog": []}\n```'
    assert architect._extract_backlog(text) is None


def test_extract_backlog_item_missing_required_field_returns_none():
    # Missing "description".
    text = '```json\n{"backlog": [{"id": "a", "title": "T1"}]}\n```'
    assert architect._extract_backlog(text) is None


def test_extract_backlog_picks_last_valid_block_when_multiple():
    text = (
        "```json\n"
        '{"backlog": [{"id": "EXAMPLE", "title": "T1", "description": "D1"}]}\n'
        "```\n\n"
        "Final answer:\n\n"
        "```json\n"
        '{"backlog": [{"id": "real", "title": "Real", "description": "D"}]}\n'
        "```"
    )
    out = architect._extract_backlog(text)
    assert out is not None
    assert out[0]["id"] == "real"


@pytest.mark.asyncio
async def test_run_initial_writes_architecture_md_and_backlog(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    parent_id = parent.id

    stub_output = (
        "Plan: I'll build a tiny TODO app.\n\n"
        "```json\n"
        '{"backlog": ['
        '{"id": "w1", "title": "Add TODO list page", "description": "..."},'
        '{"id": "w2", "title": "Persist TODOs to localStorage", "description": "..."}'
        "]}\n"
        "```"
    )
    stub_result = MagicMock(output=stub_output, tool_calls=[])

    loop = MagicMock()
    loop.run = AsyncMock(return_value=stub_result)
    loop.tool_call_log = []

    with (
        patch.object(architect, "async_session", _patched_async_session(session)),
        patch.object(architect, "create_architect_agent", return_value=loop),
        patch.object(
            architect, "_prepare_parent_workspace",
            new=AsyncMock(return_value="/tmp/ws"),
        ),
        patch.object(
            architect, "_commit_and_open_initial_pr",
            new=AsyncMock(return_value="deadbeef"),
        ),
    ):
        await architect.run_initial(parent_id)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.trio_backlog is not None
    assert len(refreshed.trio_backlog) == 2
    assert refreshed.trio_backlog[0]["title"] == "Add TODO list page"

    rows = (
        await session.execute(
            select(ArchitectAttempt).where(ArchitectAttempt.task_id == parent_id)
        )
    ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.phase == ArchitectPhase.INITIAL
    assert row.cycle == 1
    assert row.commit_sha == "deadbeef"
    assert "TODO app" in row.reasoning


@pytest.mark.asyncio
async def test_run_initial_marks_parent_blocked_on_invalid_json(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    parent_id = parent.id
    stub_result = MagicMock(output="I refuse to output JSON.", tool_calls=[])

    loop = MagicMock()
    loop.run = AsyncMock(return_value=stub_result)
    loop.tool_call_log = []

    with (
        patch.object(architect, "async_session", _patched_async_session(session)),
        patch.object(architect, "create_architect_agent", return_value=loop),
        patch.object(
            architect, "_prepare_parent_workspace",
            new=AsyncMock(return_value="/tmp/ws"),
        ),
    ):
        await architect.run_initial(parent_id)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.BLOCKED

    rows = (
        await session.execute(
            select(ArchitectAttempt).where(ArchitectAttempt.task_id == parent_id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].phase == ArchitectPhase.INITIAL
    assert rows[0].decision is not None
    assert rows[0].decision.get("action") == "blocked"
