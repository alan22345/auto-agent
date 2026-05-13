"""Load-bearing regression: the trio reviewer must reject demo-broken builds
even when all upstream agents claim success.

Failure mode this test guards against: "everybody agrees, but the result is bad."

This test runs the real reviewer agent against a stubbed workspace that
contains an obvious flaw (Lorem Ipsum on the home page). The architect and
verify pipeline are stubbed to claim success. We assert the reviewer notices
the flaw and emits ok=false.

The test requires:
  - DATABASE_URL pointing at a writable Postgres with trio columns migrated.
  - At least one live LLM provider (ANTHROPIC_API_KEY or AWS_BEARER_TOKEN_BEDROCK).
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import inspect, select

from shared.models import (
    Organization,
    Plan,
    Task,
    TaskComplexity,
    TaskSource,
    TaskStatus,
    TrioReviewAttempt,
)

# ---------------------------------------------------------------------------
# Skip predicates — evaluated at collection time so the skip message is clear.
# ---------------------------------------------------------------------------

_has_llm = bool(
    os.environ.get("ANTHROPIC_API_KEY")
    or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
)

pytestmark = pytest.mark.skipif(
    not _has_llm,
    reason="requires live LLM provider (set ANTHROPIC_API_KEY or AWS_BEARER_TOKEN_BEDROCK)",
)


# ---------------------------------------------------------------------------
# Helpers shared with other trio tests (copy-of-conftest pattern)
# ---------------------------------------------------------------------------


async def _skip_if_trio_columns_missing(session) -> None:
    """Skip cleanly when the DB hasn't run the trio migration."""
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set — trio DB tests need real Postgres")

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


def _patched_async_session(real_session):
    """Yield ``real_session`` from ``async with async_session() as s``.

    Forwards ``commit`` to ``flush`` so writes are visible inside the
    per-test savepoint that rolls back at teardown.
    """
    real_session.commit = AsyncMock(side_effect=lambda: real_session.flush())
    real_session.close = AsyncMock()
    real_session.refresh = AsyncMock()

    @asynccontextmanager
    async def _factory():
        yield real_session

    return _factory


# ---------------------------------------------------------------------------
# The load-bearing regression test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trio_reviewer_rejects_lorem_ipsum_home_page(tmp_path, session):
    """Reviewer must say ok=false when shown a workspace with Lorem Ipsum body.

    Upstream agents are ALL stubbed to claim success. Only the real reviewer
    LLM runs. This guards the "everybody agrees but the result is broken"
    failure mode.
    """
    await _skip_if_trio_columns_missing(session)

    # ------------------------------------------------------------------
    # 1. Seed parent + child tasks via the real DB session.
    # ------------------------------------------------------------------
    org = await _seed_org(session)

    parent = Task(
        title="Build TODO app",
        description="Build a TODO list app with a home page that shows the list.",
        source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_EXECUTING,
        complexity=TaskComplexity.COMPLEX,
        organization_id=org.id,
        created_by_user_id=None,
    )
    session.add(parent)
    await session.flush()

    child = Task(
        title="Add the TODO list page",
        description=(
            "Add the TODO list page at / which renders the list and an input "
            "that lets the user add a new item. The page must display real "
            "content — the heading should say 'My TODO List' and the page "
            "must show at least an input field and an empty list."
        ),
        source=TaskSource.MANUAL,
        status=TaskStatus.TRIO_REVIEW,
        complexity=TaskComplexity.COMPLEX,
        organization_id=org.id,
        parent_task_id=parent.id,
        created_by_user_id=None,
    )
    session.add(child)
    await session.flush()
    child_id = child.id

    # ------------------------------------------------------------------
    # 2. Populate the fake workspace with ARCHITECTURE.md + a flawed page.
    # ------------------------------------------------------------------
    (tmp_path / "ARCHITECTURE.md").write_text(
        "# TODO App\n\n"
        "## Routes\n"
        "- `/` — TODO list page with add-item input and a list of existing items.\n\n"
        "## Stack\n"
        "- Next.js App Router with TypeScript\n"
        "- Tailwind CSS for styling\n"
    )
    # The obvious flaw: Lorem Ipsum where the description promised real content.
    (tmp_path / "page.tsx").write_text(
        "export default function Home() {\n"
        "  return (\n"
        "    <main>\n"
        "      <h1>Lorem ipsum dolor sit amet</h1>\n"
        "      <p>Consectetur adipiscing elit, sed do eiusmod.</p>\n"
        "    </main>\n"
        "  );\n"
        "}\n"
    )
    # Minimal git repo so `git diff` works without error.
    import subprocess
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "add page"],
        check=True, capture_output=True,
    )

    # ------------------------------------------------------------------
    # 3. Patch infrastructure seams so the reviewer doesn't need live DB
    #    connections beyond the test's session, no real repo, no PR opening.
    # ------------------------------------------------------------------
    import agent.lifecycle.trio.reviewer as reviewer_mod

    factory = _patched_async_session(session)

    with (
        patch.object(reviewer_mod, "async_session", factory),
        patch(
            "agent.lifecycle.trio.reviewer.home_dir_for_task",
            new=AsyncMock(return_value=None),
        ),
        # _open_pr_and_advance must not run — ok=false should never reach it,
        # but guard against any test-path divergence.
        patch(
            "agent.lifecycle.coding._open_pr_and_advance",
            new=AsyncMock(),
        ),
    ):
        # Pass workspace directly → bypasses _prepare_review_workspace entirely.
        await reviewer_mod.handle_trio_review(
            child_id,
            workspace=str(tmp_path),
            parent_branch="HEAD~1",  # parent_branch for git diff; shows our commit
        )

    # ------------------------------------------------------------------
    # 4. Assert the reviewer caught Lorem Ipsum and emitted ok=false.
    # ------------------------------------------------------------------
    rows = (
        await session.execute(
            select(TrioReviewAttempt).where(TrioReviewAttempt.task_id == child_id)
        )
    ).scalars().all()

    assert len(rows) >= 1, (
        f"Expected at least 1 review attempt row for child {child_id}; "
        f"got {len(rows)}. The reviewer may have returned early without "
        f"writing a verdict."
    )
    last = rows[-1]
    assert last.ok is False, (
        f"Reviewer should have rejected the Lorem Ipsum placeholder but "
        f"emitted ok=true.\n"
        f"Feedback was: {last.feedback!r}\n\n"
        f"This is the load-bearing regression: the reviewer prompt is too "
        f"weak to catch obvious placeholder content. Tighten TRIO_REVIEWER_SYSTEM "
        f"in agent/lifecycle/trio/prompts.py — add explicit instructions to "
        f"reject Lorem Ipsum / placeholder text where the work item promised "
        f"real content."
    )

    # Child must have been looped back to CODING so the builder can fix it.
    refreshed_child = (
        await session.execute(select(Task).where(Task.id == child_id))
    ).scalar_one()
    assert refreshed_child.status == TaskStatus.CODING, (
        f"Expected child to transition back to CODING after reviewer rejection, "
        f"but status is {refreshed_child.status!r}."
    )
