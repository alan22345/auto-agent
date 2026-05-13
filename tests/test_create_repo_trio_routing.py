"""Tests for Task-22: create_repo forces complexity=COMPLEX_LARGE on scaffold
tasks, and the classifier (on_task_created) short-circuits without an LLM call
when complexity is already set.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

import run as run_module
from shared.events import Event
from shared.models import (
    Organization,
    Plan,
    Task,
    TaskComplexity,
    TaskSource,
    TaskStatus,
)

# ---------------------------------------------------------------------------
# DB-less tests — just check the create_repo module's Task kwargs
# ---------------------------------------------------------------------------


def test_create_repo_scaffold_task_uses_complex_large():
    """The Task() constructor call in create_repo.py must set
    complexity=TaskComplexity.COMPLEX_LARGE so that scaffold tasks always route
    through the trio pipeline without relying on the keyword classifier."""
    import inspect

    import orchestrator.create_repo as cr_module

    src = inspect.getsource(cr_module)
    # Ensure complexity is forced to COMPLEX_LARGE (not COMPLEX)
    assert "TaskComplexity.COMPLEX_LARGE" in src, (
        "create_repo.py must set complexity=TaskComplexity.COMPLEX_LARGE "
        "on the scaffold Task"
    )
    assert "complexity=TaskComplexity.COMPLEX_LARGE" in src, (
        "Expected `complexity=TaskComplexity.COMPLEX_LARGE` in create_repo.py"
    )


def test_create_repo_scaffold_task_has_freeform_mode():
    """The scaffold task must also have freeform_mode=True."""
    import inspect

    import orchestrator.create_repo as cr_module

    src = inspect.getsource(cr_module)
    assert "freeform_mode=True" in src, (
        "create_repo.py must set freeform_mode=True on the scaffold Task"
    )


# ---------------------------------------------------------------------------
# DB-backed tests — verify the classifier short-circuit in on_task_created
# ---------------------------------------------------------------------------


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


async def _seed_intake_task(
    session,
    *,
    complexity: TaskComplexity | None,
    freeform_mode: bool = False,
) -> Task:
    """Seed a task in INTAKE status (the state on_task_created expects)."""
    org = await _seed_org(session)
    t = Task(
        title="Build scaffold",
        description="Bootstrap the repo",
        source=TaskSource.MANUAL,
        status=TaskStatus.INTAKE,
        complexity=complexity,
        organization_id=org.id,
        freeform_mode=freeform_mode,
        created_by_user_id=None,  # skip the dispatch-time auth probe
    )
    session.add(t)
    await session.flush()
    return t


def _patched_async_session(real_session):
    """Yield real_session, forwarding commit → flush so writes are visible
    inside the test transaction and roll back at teardown."""
    real_session.commit = AsyncMock(side_effect=lambda: real_session.flush())
    real_session.close = AsyncMock()
    real_session.refresh = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


async def _skip_if_schema_not_ready(session) -> None:
    """Skip when DATABASE_URL is absent, SQLite, or the tasks table hasn't
    been migrated to include the trio + verify/review columns.

    Mirrors the guard used in test_trio_routing.py so these tests skip cleanly
    when the local DB hasn't run all alembic migrations.
    """
    from sqlalchemy import inspect

    url = os.environ.get("DATABASE_URL", "")
    if not url or "sqlite" in url or "memory" in url:
        pytest.skip("Needs real Postgres — DATABASE_URL not set or is SQLite")

    # Verify the tasks table has the columns this test writes.
    required_cols = {"complexity", "freeform_mode", "trio_phase"}

    def _check(sync_conn) -> set[str]:
        insp = inspect(sync_conn)
        try:
            cols = {c["name"] for c in insp.get_columns("tasks")}
            return cols & required_cols
        except Exception:
            return set()

    conn = await session.connection()
    present = await conn.run_sync(_check)
    if len(present) < len(required_cols):
        missing = required_cols - present
        pytest.skip(
            f"tasks table missing columns {missing} — run `alembic upgrade head`"
        )


@pytest.mark.asyncio
async def test_classifier_skips_llm_when_complexity_already_set(session, monkeypatch):
    """When a task already has complexity set, on_task_created must skip the
    classify_task() call and still publish task_classified so the pipeline
    continues."""
    await _skip_if_schema_not_ready(session)

    task = await _seed_intake_task(
        session,
        complexity=TaskComplexity.COMPLEX_LARGE,
        freeform_mode=True,
    )
    task_id = task.id

    monkeypatch.setattr(run_module, "async_session", _patched_async_session(session))
    # Stub out repo matching — we don't want network/DB side-effects.
    monkeypatch.setattr(run_module, "match_repo", AsyncMock(return_value=None))

    classify_task_mock = AsyncMock()
    published_events: list[Event] = []

    async def _capture_publish(event: Event) -> None:
        published_events.append(event)

    with patch("run.classify_task", new=classify_task_mock):
        monkeypatch.setattr(run_module, "publish", _capture_publish)
        await run_module.on_task_created(Event(type="created", task_id=task_id))

    # The LLM classifier must NOT have been called.
    classify_task_mock.assert_not_called()

    # A task_classified event must have been published (pipeline continues).
    classified_types = [e.type for e in published_events]
    assert any("classified" in t for t in classified_types), (
        f"Expected a task_classified event, got: {classified_types}"
    )

    # The task should now be in CLASSIFYING (on_task_created transitions it
    # to CLASSIFYING before publishing; on_task_classified is a separate handler).
    refreshed = (
        await session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()
    assert refreshed.status == TaskStatus.CLASSIFYING
    # Complexity must still be COMPLEX_LARGE (not clobbered).
    assert refreshed.complexity == TaskComplexity.COMPLEX_LARGE


@pytest.mark.asyncio
async def test_classifier_calls_llm_when_complexity_not_set(session, monkeypatch):
    """When complexity is None, on_task_created must call the classifier
    (this verifies the short-circuit only fires when complexity is pre-set)."""
    await _skip_if_schema_not_ready(session)

    from shared.types import ClassificationResult

    task = await _seed_intake_task(session, complexity=None)
    task_id = task.id

    monkeypatch.setattr(run_module, "async_session", _patched_async_session(session))
    monkeypatch.setattr(run_module, "match_repo", AsyncMock(return_value=None))

    classify_task_mock = AsyncMock(
        return_value=(
            TaskComplexity.COMPLEX,
            ClassificationResult(
                classification="complex",
                reasoning="stub",
                estimated_files=3,
                risk="medium",
            ),
        )
    )

    published_events: list[Event] = []

    async def _capture_publish(event: Event) -> None:
        published_events.append(event)

    with patch("run.classify_task", new=classify_task_mock):
        monkeypatch.setattr(run_module, "publish", _capture_publish)
        await run_module.on_task_created(Event(type="created", task_id=task_id))

    # Classifier must have been called once.
    classify_task_mock.assert_called_once()
