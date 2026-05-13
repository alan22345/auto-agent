"""Tests for ``architect.consult`` — the architect's mid-build Q&A.

Mirrors the pattern in ``test_architect_run_initial``: the outside-world
seams (workspace prep, consult-doc-update PR plumbing, the agent loop)
are patched, but the real DB session is used so we exercise the
``architect_attempts`` row writes against the real schema.

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
    """Skip the test if the connected DB hasn't run the trio migration."""
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
        title="Build app",
        description="Build app",
        source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING,
        complexity=TaskComplexity.COMPLEX_LARGE,
        trio_phase=TrioPhase.AWAITING_BUILDER,
        organization_id=org.id,
    )
    session.add(t)
    await session.flush()
    return t


async def _seed_child(session, parent: Task, description: str = "auth") -> Task:
    t = Task(
        title=description,
        description=description,
        source=TaskSource.MANUAL,
        status=TaskStatus.PENDING,
        complexity=TaskComplexity.SIMPLE,
        organization_id=parent.organization_id,
        parent_task_id=parent.id,
    )
    session.add(t)
    await session.flush()
    return t


def _patched_async_session(real_session):
    """Build a factory that yields ``real_session`` from ``async with``."""
    real_session.commit = AsyncMock(side_effect=lambda: real_session.flush())
    real_session.close = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


def test_extract_consult_payload_happy_path():
    text = (
        "Use Postgres.\n\n"
        '```json\n{"answer": "Use Postgres.", "architecture_md_updated": false}\n```'
    )
    out = architect._extract_consult_payload(text)
    assert out is not None
    assert out["answer"] == "Use Postgres."
    assert out["architecture_md_updated"] is False


def test_extract_consult_payload_defaults_architecture_md_updated():
    text = '```json\n{"answer": "ok"}\n```'
    out = architect._extract_consult_payload(text)
    assert out is not None
    assert out["architecture_md_updated"] is False


def test_extract_consult_payload_missing_block_returns_none():
    assert architect._extract_consult_payload("just prose") is None
    assert architect._extract_consult_payload("") is None


def test_extract_consult_payload_malformed_json_returns_none():
    assert architect._extract_consult_payload("```json\n{not valid}\n```") is None


def test_extract_consult_payload_missing_answer_returns_none():
    text = '```json\n{"architecture_md_updated": true}\n```'
    assert architect._extract_consult_payload(text) is None


@pytest.mark.asyncio
async def test_consult_returns_answer_and_persists_row(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    child = await _seed_child(session, parent)
    parent_id = parent.id
    child_id = child.id

    stub_output = (
        "Use Postgres for multi-user.\n\n"
        '```json\n{"answer": "Use Postgres.", "architecture_md_updated": false}\n```'
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
    ):
        result = await architect.consult(
            parent_task_id=parent_id,
            child_task_id=child_id,
            question="Which db?",
            why="Choosing between Postgres and SQLite.",
        )

    assert result == {"answer": "Use Postgres.", "architecture_md_updated": False}

    rows = (
        await session.execute(
            select(ArchitectAttempt).where(ArchitectAttempt.task_id == parent_id)
        )
    ).scalars().all()
    consult_rows = [r for r in rows if r.phase == ArchitectPhase.CONSULT]
    assert len(consult_rows) == 1
    row = consult_rows[0]
    assert row.consult_question == "Which db?"
    assert row.consult_why == "Choosing between Postgres and SQLite."
    assert row.cycle == 1
    assert row.commit_sha is None


@pytest.mark.asyncio
async def test_consult_commits_when_architecture_md_updated(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    child = await _seed_child(session, parent)
    parent_id = parent.id
    child_id = child.id

    stub_output = (
        "Updated ARCHITECTURE.md to clarify db choice.\n\n"
        '```json\n{"answer": "Use Postgres.", "architecture_md_updated": true}\n```'
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
            architect, "_commit_consult_doc_update",
            new=AsyncMock(return_value="cafef00d"),
        ) as commit_mock,
    ):
        result = await architect.consult(
            parent_task_id=parent_id,
            child_task_id=child_id,
            question="x",
            why="x",
        )
        commit_mock.assert_awaited_once()

    assert result["architecture_md_updated"] is True

    rows = (
        await session.execute(
            select(ArchitectAttempt).where(ArchitectAttempt.task_id == parent_id)
        )
    ).scalars().all()
    consult_rows = [r for r in rows if r.phase == ArchitectPhase.CONSULT]
    assert len(consult_rows) == 1
    assert consult_rows[0].commit_sha == "cafef00d"


@pytest.mark.asyncio
async def test_consult_cycle_increments_on_repeat_calls(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    child = await _seed_child(session, parent)
    parent_id = parent.id
    child_id = child.id

    stub_output = (
        '```json\n{"answer": "ok", "architecture_md_updated": false}\n```'
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
    ):
        await architect.consult(
            parent_task_id=parent_id, child_task_id=child_id,
            question="q1", why="w1",
        )
        await architect.consult(
            parent_task_id=parent_id, child_task_id=child_id,
            question="q2", why="w2",
        )

    rows = (
        await session.execute(
            select(ArchitectAttempt)
            .where(ArchitectAttempt.task_id == parent_id)
            .order_by(ArchitectAttempt.cycle)
        )
    ).scalars().all()
    consult_rows = [r for r in rows if r.phase == ArchitectPhase.CONSULT]
    assert [r.cycle for r in consult_rows] == [1, 2]
    assert [r.consult_question for r in consult_rows] == ["q1", "q2"]
