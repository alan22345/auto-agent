"""Tests for ``architect.checkpoint`` and ``architect.run_revision``.

Mirrors the pattern in ``test_architect_consult`` /
``test_architect_run_initial``: the outside-world seams (workspace prep,
the PR plumbing, the agent loop) are patched, but the real DB session
is used so we exercise the ``architect_attempts`` row writes and the
``Task.trio_backlog`` mutation against the real schema.

``architect.async_session`` is patched to a factory that yields the
test's transaction-wrapped session so every write the implementation
makes lands inside the per-test savepoint and rolls back cleanly.
"""
from __future__ import annotations

import json
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


async def _seed_parent(
    session,
    *,
    trio_phase: TrioPhase = TrioPhase.ARCHITECT_CHECKPOINT,
) -> Task:
    org = await _seed_org(session)
    t = Task(
        title="Build app",
        description="Build app",
        source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING,
        complexity=TaskComplexity.COMPLEX_LARGE,
        trio_phase=trio_phase,
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
        status=TaskStatus.DONE,
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


def _checkpoint_response(backlog: list[dict], action: str = "continue") -> str:
    return (
        "Reviewed the merge. Continuing.\n\n"
        f"```json\n{json.dumps({'backlog': backlog, 'decision': {'action': action, 'reason': ''}})}\n```"
    )


# ---------------------------------------------------------------------------
# _extract_checkpoint_payload unit tests (no DB required).
# ---------------------------------------------------------------------------


def test_extract_checkpoint_payload_happy_path():
    text = (
        "Looks good.\n\n"
        '```json\n{"backlog": [{"id": "w1", "title": "T", "description": "D"}], '
        '"decision": {"action": "continue", "reason": "ok"}}\n```'
    )
    out = architect._extract_checkpoint_payload(text)
    assert out is not None
    assert out["decision"]["action"] == "continue"
    assert out["backlog"][0]["id"] == "w1"


def test_extract_checkpoint_payload_missing_block_returns_none():
    assert architect._extract_checkpoint_payload("just prose") is None
    assert architect._extract_checkpoint_payload("") is None


def test_extract_checkpoint_payload_malformed_json_returns_none():
    assert architect._extract_checkpoint_payload(
        "```json\n{not valid}\n```"
    ) is None


def test_extract_checkpoint_payload_missing_decision_returns_none():
    text = '```json\n{"backlog": []}\n```'
    assert architect._extract_checkpoint_payload(text) is None


def test_extract_checkpoint_payload_missing_backlog_returns_none():
    text = '```json\n{"decision": {"action": "continue"}}\n```'
    assert architect._extract_checkpoint_payload(text) is None


def test_extract_checkpoint_payload_decision_missing_action_returns_none():
    text = '```json\n{"backlog": [], "decision": {"reason": "ok"}}\n```'
    assert architect._extract_checkpoint_payload(text) is None


# ---------------------------------------------------------------------------
# checkpoint integration tests (DB-backed).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_child_done_updates_backlog_and_persists_row(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    parent.trio_backlog = [
        {"id": "w1", "title": "auth", "description": "...", "status": "in_progress", "assigned_task_id": 99},
        {"id": "w2", "title": "ingredients", "description": "...", "status": "pending"},
    ]
    await session.flush()
    child = await _seed_child(session, parent)
    parent_id = parent.id
    child_id = child.id

    new_backlog = [
        {"id": "w1", "title": "auth", "description": "...", "status": "done", "assigned_task_id": 99},
        {"id": "w2", "title": "ingredients", "description": "...", "status": "pending"},
    ]
    stub_output = _checkpoint_response(new_backlog, action="continue")
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
        decision = await architect.checkpoint(parent_id, child_task_id=child_id)

    assert decision["action"] == "continue"

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.trio_backlog[0]["status"] == "done"

    rows = (
        await session.execute(
            select(ArchitectAttempt).where(ArchitectAttempt.task_id == parent_id)
        )
    ).scalars().all()
    cp = [r for r in rows if r.phase == ArchitectPhase.CHECKPOINT]
    assert len(cp) == 1
    assert cp[0].decision["action"] == "continue"
    assert cp[0].cycle == 1


@pytest.mark.asyncio
async def test_checkpoint_repair_context_threads_ci_log_and_adds_fix_items(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    parent.trio_backlog = [
        {"id": "w1", "title": "auth", "description": "...", "status": "done"},
    ]
    await session.flush()
    parent_id = parent.id

    repair_ctx = {
        "ci_log": "TypeError: cannot import name 'foo'",
        "failed_pr_url": "https://github.com/x/y/pull/3",
    }
    new_backlog = [
        {"id": "w1", "title": "auth", "description": "...", "status": "done"},
        {"id": "wfix", "title": "fix import error", "description": "...", "status": "pending"},
    ]
    stub_output = _checkpoint_response(new_backlog, action="continue")
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
        decision = await architect.checkpoint(
            parent_id, repair_context=repair_ctx,
        )

    assert decision["action"] == "continue"

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    ids = [item["id"] for item in refreshed.trio_backlog]
    assert "wfix" in ids

    # The CI log must have been threaded into the agent's task description.
    create_call = loop.run.await_args
    prompt = create_call.args[0] if create_call.args else ""
    assert "failed CI" in prompt
    assert "TypeError" in prompt
    assert "https://github.com/x/y/pull/3" in prompt


@pytest.mark.asyncio
async def test_checkpoint_invalid_json_persists_blocked_decision(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    parent.trio_backlog = [
        {"id": "w1", "title": "auth", "description": "...", "status": "in_progress"},
    ]
    await session.flush()
    original_backlog = list(parent.trio_backlog)
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
        decision = await architect.checkpoint(parent_id, child_task_id=None)

    assert decision == {"action": "blocked", "reason": "invalid checkpoint JSON"}

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    # Backlog left untouched when extraction fails.
    assert refreshed.trio_backlog == original_backlog

    rows = (
        await session.execute(
            select(ArchitectAttempt).where(ArchitectAttempt.task_id == parent_id)
        )
    ).scalars().all()
    cp = [r for r in rows if r.phase == ArchitectPhase.CHECKPOINT]
    assert len(cp) == 1
    assert cp[0].decision["action"] == "blocked"


@pytest.mark.asyncio
async def test_checkpoint_cycle_increments_on_repeat_calls(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session)
    parent.trio_backlog = [
        {"id": "w1", "title": "auth", "description": "...", "status": "in_progress"},
    ]
    await session.flush()
    parent_id = parent.id

    stub_output = _checkpoint_response(
        [{"id": "w1", "title": "auth", "description": "...", "status": "done"}],
        action="continue",
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
        await architect.checkpoint(parent_id, child_task_id=None)
        await architect.checkpoint(parent_id, child_task_id=None)

    rows = (
        await session.execute(
            select(ArchitectAttempt)
            .where(ArchitectAttempt.task_id == parent_id)
            .order_by(ArchitectAttempt.cycle)
        )
    ).scalars().all()
    cp = [r for r in rows if r.phase == ArchitectPhase.CHECKPOINT]
    assert [r.cycle for r in cp] == [1, 2]


# ---------------------------------------------------------------------------
# run_revision integration tests (DB-backed).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_revision_rewrites_architecture_md_and_backlog(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session, trio_phase=TrioPhase.ARCHITECTING)
    parent_id = parent.id

    stub_output = (
        "Re-thought the design.\n\n"
        "```json\n"
        '{"backlog": [{"id": "v1", "title": "new shape", "description": "..."}]}\n'
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
            new=AsyncMock(return_value="deadcafe"),
        ),
    ):
        await architect.run_revision(parent_id)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.trio_backlog is not None
    assert refreshed.trio_backlog[0]["id"] == "v1"

    rows = (
        await session.execute(
            select(ArchitectAttempt).where(ArchitectAttempt.task_id == parent_id)
        )
    ).scalars().all()
    rev = [r for r in rows if r.phase == ArchitectPhase.REVISION]
    assert len(rev) == 1
    assert rev[0].cycle == 1
    assert rev[0].commit_sha == "deadcafe"


@pytest.mark.asyncio
async def test_run_revision_marks_parent_blocked_on_invalid_json(session):
    await _skip_if_trio_columns_missing(session)
    parent = await _seed_parent(session, trio_phase=TrioPhase.ARCHITECTING)
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
        await architect.run_revision(parent_id)

    refreshed = (
        await session.execute(select(Task).where(Task.id == parent_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.BLOCKED

    rows = (
        await session.execute(
            select(ArchitectAttempt).where(ArchitectAttempt.task_id == parent_id)
        )
    ).scalars().all()
    rev = [r for r in rows if r.phase == ArchitectPhase.REVISION]
    assert len(rev) == 1
    assert rev[0].decision is not None
    assert rev[0].decision.get("action") == "blocked"
