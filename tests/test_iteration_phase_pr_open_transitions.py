"""ADR-017 — after opening the integration PR, trio auto-falls-through
PR_CREATED → AWAITING_REVIEW. PR_CREATED becomes a single-fire transit
event; AWAITING_REVIEW is the long-lived "PR open" state."""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires DATABASE_URL",
)
async def test_open_integration_pr_falls_through_to_awaiting_review(session):
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
        name=f"r-{suffix}", url="https://github.com/o/r.git",
        organization_id=org.id, default_branch="main",
    )
    session.add(repo)
    await session.flush()
    parent = Task(
        title="P", description="d", source=TaskSource.MANUAL,
        status=TaskStatus.FINAL_REVIEW,
        complexity=TaskComplexity.COMPLEX_LARGE,
        repo_id=repo.id, organization_id=org.id,
    )
    session.add(parent)
    await session.commit()

    from agent.lifecycle.trio import _open_integration_pr_and_transition

    with patch(
        "agent.lifecycle.trio._open_integration_pr",
        AsyncMock(return_value="https://github.com/o/r/pull/42"),
    ):
        await _open_integration_pr_and_transition(parent=parent, target_branch="main")

    await session.refresh(parent)
    assert parent.status == TaskStatus.AWAITING_REVIEW, (
        f"expected fall-through to AWAITING_REVIEW, got {parent.status}"
    )
