"""ADR-017 — architect.iterate produces a fresh backlog appended to the
existing trio_backlog, with the user feedback + PR diff + design.md in
the pinned context."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_iterate_appends_to_existing_backlog(session):
    from sqlalchemy import select

    from agent.lifecycle.trio import architect
    from shared.models import (
        Organization,
        Repo,
        Task,
        TaskComplexity,
        TaskSource,
        TaskStatus,
    )
    from tests.helpers import _ensure_default_plan

    plan = await _ensure_default_plan(session)
    suffix = uuid.uuid4().hex[:8]
    org = Organization(name=f"r-{suffix}", slug=f"r-{suffix}", plan_id=plan.id)
    session.add(org)
    await session.flush()
    repo = Repo(
        name=f"r-{suffix}",
        url="https://github.com/o/r.git",
        organization_id=org.id,
        default_branch="main",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P",
        description="d",
        source=TaskSource.MANUAL,
        status=TaskStatus.ITERATING,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id,
        organization_id=org.id,
        trio_backlog=[
            {"id": "S1", "title": "x", "description": "", "status": "done", "head_sha": "abc"},
        ],
    )
    session.add(parent)
    await session.commit()

    # The agent output uses the ```json {"backlog": [...]} ``` envelope that
    # _extract_backlog knows how to parse — no mock needed for the extractor.
    stub_output = (
        "Re-iteration backlog:\n\n"
        "```json\n"
        '{"backlog": [{"id": "S2", "title": "address feedback", '
        '"description": "...", "status": "pending"}]}\n'
        "```"
    )
    stub_result = MagicMock(output=stub_output, tool_calls=[])

    agent_loop = MagicMock()
    agent_loop.run = AsyncMock(return_value=stub_result)
    agent_loop.tool_call_log = []

    # Stub everything that touches workspace / shell / LLM — we test the
    # architect.iterate plumbing, not the agent loop.
    with (
        patch.object(architect, "_prepare_parent_workspace", AsyncMock(return_value="/tmp/ws")),
        patch("agent.lifecycle.trio.architect._read_design_md", return_value="design"),
        patch("agent.lifecycle.trio.architect._read_pr_diff", AsyncMock(return_value="diff text")),
        patch.object(architect, "create_architect_agent", return_value=agent_loop),
        patch.object(architect, "home_dir_for_task", AsyncMock(return_value="/tmp/home")),
        patch.object(architect, "async_session") as mock_async_session,
    ):
        # Wire the patched async_session to our test session so DB writes
        # land inside the per-test savepoint and roll back cleanly.
        from contextlib import asynccontextmanager

        session.commit = AsyncMock(side_effect=lambda: session.flush())
        session.close = AsyncMock()

        @asynccontextmanager
        async def _session_factory():
            yield session

        mock_async_session.side_effect = _session_factory

        await architect.iterate(
            parent.id,
            iteration_context={"feedback": "break it down further", "pr_url": "https://x/pull/1"},
        )

    refreshed = (await session.execute(select(Task).where(Task.id == parent.id))).scalar_one()
    ids = [item["id"] for item in (refreshed.trio_backlog or [])]
    assert "S1" in ids, "existing done item dropped"
    assert "S2" in ids, "new pending item not appended"
    assert next(i for i in refreshed.trio_backlog if i["id"] == "S2")["status"] == "pending"
