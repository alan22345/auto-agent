"""Phase 7.5 — ``run_trio_parent`` honours the design-doc approval gate.

ADR-015 §2 / Phase 6 introduced ``architect.run_design`` + the
``AWAITING_DESIGN_APPROVAL`` gate, but Phase 7 left the dispatcher still
calling ``architect.run_initial`` from ``run_trio_parent``. The result:
every complex_large task on the VM enters TRIO_EXECUTING, the architect
emits a flat backlog directly with no design.md ever written, and the
design-approval UI is dead. Phase 7.5 rewires the front half.

Five behaviours pinned here — each maps to one branch of the new flow:

1. Fresh complex_large trio parent (no design.md, no backlog):
   ``run_trio_parent`` calls ``architect.run_design`` (NOT
   ``run_initial``) and returns without dispatching any items. The
   architect itself transitions the task to AWAITING_DESIGN_APPROVAL via
   ``finalize_design``.

2. Re-entry with design.md present + plan_approval.json approved:
   ``run_trio_parent`` skips ``run_design``, calls
   ``architect.run_initial`` (the backlog-emit step), and proceeds into
   the per-item loop.

3. Re-entry with design.md present + plan_approval.json rejected: the
   parent transitions to BLOCKED via ``resume_after_design_approval``.
   No further architect calls fire.

4. Freeform mode at the design gate: ``run_freeform_gate`` fires the
   standin, ``plan_approval.json`` is auto-written with verdict=approved,
   resume_after_design_approval transitions to ARCHITECT_BACKLOG_EMIT,
   and the flow continues into ``run_initial``.

5. Human-in-loop mode at the design gate + missing approval file:
   ``run_trio_parent`` returns without invoking ``run_initial``. The
   task stays in AWAITING_DESIGN_APPROVAL — the orchestrator handler
   re-enters when the user approves.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import agent.lifecycle.trio as trio
from agent.lifecycle.trio import architect as architect_mod
from agent.lifecycle.trio import design_approval as design_approval_mod
from agent.lifecycle.workspace_paths import DESIGN_PATH, PLAN_APPROVAL_PATH

# ---------------------------------------------------------------------------
# Fakes — minimal shims for Task + the async_session() context manager so
# the wiring tests don't need a live Postgres.
# ---------------------------------------------------------------------------


def _make_fake_task(
    *,
    task_id: int = 1,
    status: str = "trio_executing",
    complexity: str = "complex_large",
    trio_backlog: list[dict] | None = None,
    freeform_mode: bool = False,
    mode_override: str | None = None,
    organization_id: int = 7,
    repo_id: int | None = None,
):
    """Build a SimpleNamespace with the attributes ``run_trio_parent`` reads.

    Using ``SimpleNamespace`` keeps the test independent of the ORM —
    ``run_trio_parent`` only reads attributes; it never invokes SQLAlchemy
    state on the Task object itself.
    """

    from shared.models import TaskComplexity, TaskStatus

    complexity_enum = (
        TaskComplexity.COMPLEX_LARGE if complexity == "complex_large" else TaskComplexity.COMPLEX
    )
    status_enum = TaskStatus(status)

    return SimpleNamespace(
        id=task_id,
        status=status_enum,
        complexity=complexity_enum,
        trio_backlog=trio_backlog,
        trio_phase=None,
        freeform_mode=freeform_mode,
        mode_override=mode_override,
        organization_id=organization_id,
        repo_id=repo_id,
        repo=None,
        title="Build app",
        description="Build app",
        pr_url=None,
        created_by_user_id=None,
        source_id="",
    )


def _patched_async_session_factory(fake_task):
    """Return a context-manager factory whose ``execute`` yields ``fake_task``.

    Mirrors the ``_patched_async_session`` shape used in
    ``test_trio_orchestrator.py`` but reads from / writes to an in-memory
    shim rather than a real DB.
    """

    @asynccontextmanager
    async def factory():
        class _ExecResult:
            def __init__(self, task):
                self._task = task

            def scalar_one(self):
                return self._task

            def scalar_one_or_none(self):
                return self._task

        class _Session:
            def __init__(self, task):
                self._task = task
                self.committed = False

            async def execute(self, *_a, **_kw):
                return _ExecResult(self._task)

            def add(self, _obj):
                pass

            async def commit(self):
                self.committed = True

            async def flush(self):
                pass

        yield _Session(fake_task)

    return factory


# ---------------------------------------------------------------------------
# 1. Fresh complex_large parent → run_design, never run_initial.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_complex_large_calls_run_design_not_run_initial(tmp_path):
    fake_task = _make_fake_task(complexity="complex_large", trio_backlog=None)

    run_design_mock = AsyncMock()
    run_initial_mock = AsyncMock()

    with (
        patch.object(trio, "async_session", _patched_async_session_factory(fake_task)),
        patch.object(architect_mod, "run_design", run_design_mock),
        patch.object(architect_mod, "run_initial", run_initial_mock),
        patch.object(
            architect_mod,
            "_prepare_parent_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
        patch.object(trio, "_ensure_integration_branch_checked_out", new=AsyncMock()),
        patch.object(trio, "_maybe_dispatch_sub_architects", new=AsyncMock(return_value=False)),
        patch("agent.lifecycle.factory.home_dir_for_task", new=AsyncMock(return_value=None)),
    ):
        await trio.run_trio_parent(fake_task)

    run_design_mock.assert_awaited_once()
    run_initial_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2. Re-entry after approval → run_initial (skipping run_design).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reentry_with_approved_design_calls_run_initial(tmp_path):
    """Design.md is already on disk, plan_approval.json says approved, and
    the parent landed back in ARCHITECT_BACKLOG_EMIT via the approval
    endpoint. ``run_trio_parent`` must call ``run_initial`` (the backlog-
    emit step) and continue into the per-item loop."""

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / DESIGN_PATH).write_text("# Design\n")
    (tmp_path / PLAN_APPROVAL_PATH).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "verdict": "approved",
                "comments": "",
            }
        )
    )

    fake_task = _make_fake_task(
        complexity="complex_large",
        status="architect_backlog_emit",
        trio_backlog=None,
    )

    run_design_mock = AsyncMock()
    run_initial_mock = AsyncMock()

    # After run_initial returns, the backlog is empty (None) so the
    # per-item loop exits immediately and the final review code path
    # fires. Stub the final-review driver to avoid touching that surface.
    drive_final_mock = AsyncMock()

    with (
        patch.object(trio, "async_session", _patched_async_session_factory(fake_task)),
        patch.object(architect_mod, "run_design", run_design_mock),
        patch.object(architect_mod, "run_initial", run_initial_mock),
        patch.object(
            architect_mod,
            "_prepare_parent_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
        patch.object(trio, "_ensure_integration_branch_checked_out", new=AsyncMock()),
        patch.object(trio, "_maybe_dispatch_sub_architects", new=AsyncMock(return_value=False)),
        patch("agent.lifecycle.factory.home_dir_for_task", new=AsyncMock(return_value=None)),
        patch.object(trio, "_drive_final_review_and_pr", drive_final_mock),
        patch.object(trio, "_resolve_target_branch", new=AsyncMock(return_value="main")),
    ):
        await trio.run_trio_parent(fake_task)

    run_design_mock.assert_not_awaited()
    run_initial_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. Re-entry with rejected design → resume_after_design_approval blocks.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reentry_with_rejected_design_blocks(tmp_path):
    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / DESIGN_PATH).write_text("# Design\n")
    (tmp_path / PLAN_APPROVAL_PATH).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "verdict": "rejected",
                "comments": "Stack wrong.",
            }
        )
    )

    fake_task = _make_fake_task(
        complexity="complex_large",
        status="awaiting_design_approval",
        trio_backlog=None,
    )

    run_design_mock = AsyncMock()
    run_initial_mock = AsyncMock()

    # When the rejection verdict is read, design_approval will call
    # ``transition_task`` to flip the task to BLOCKED. We mirror that on
    # the in-memory fake so the next session read inside
    # ``_advance_through_design_gate`` sees the updated status.
    from shared.models import TaskStatus

    async def fake_transition_task(task_id, to_status, *_a, **_kw):
        if to_status == "blocked":
            fake_task.status = TaskStatus.BLOCKED
        elif to_status == "architect_backlog_emit":
            fake_task.status = TaskStatus.ARCHITECT_BACKLOG_EMIT

    transition_mock = AsyncMock(side_effect=fake_transition_task)

    with (
        patch.object(trio, "async_session", _patched_async_session_factory(fake_task)),
        patch.object(architect_mod, "run_design", run_design_mock),
        patch.object(architect_mod, "run_initial", run_initial_mock),
        patch.object(
            architect_mod,
            "_prepare_parent_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
        patch.object(design_approval_mod, "transition_task", transition_mock),
        patch.object(trio, "_ensure_integration_branch_checked_out", new=AsyncMock()),
        patch.object(trio, "_maybe_dispatch_sub_architects", new=AsyncMock(return_value=False)),
        patch("agent.lifecycle.factory.home_dir_for_task", new=AsyncMock(return_value=None)),
    ):
        await trio.run_trio_parent(fake_task)

    run_design_mock.assert_not_awaited()
    run_initial_mock.assert_not_awaited()
    # design_approval's transition_task fired with "blocked" target.
    transition_mock.assert_awaited()
    args, _ = transition_mock.call_args
    assert args[1] == "blocked"


# ---------------------------------------------------------------------------
# 4. Freeform mode at the design gate → standin writes approval, flow continues.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_freeform_mode_invokes_standin_at_design_gate(tmp_path):
    """At AWAITING_DESIGN_APPROVAL in freeform mode, the standin must be
    invoked via ``run_freeform_gate`` and its plan_approval.json write
    must drive the flow forward into run_initial."""

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / DESIGN_PATH).write_text("# Design\nbody.\n")
    # NOTE: plan_approval.json is NOT pre-written — the standin must write it.

    fake_repo = SimpleNamespace(
        id=42,
        mode="freeform",
        product_brief="A todo app for testing.",
        name="test-repo",
    )
    fake_task = _make_fake_task(
        complexity="complex_large",
        status="awaiting_design_approval",
        trio_backlog=None,
        freeform_mode=True,
        mode_override="freeform",
        repo_id=42,
    )
    fake_task.repo = fake_repo

    run_design_mock = AsyncMock()
    run_initial_mock = AsyncMock()
    drive_final_mock = AsyncMock()

    async def fake_run_freeform_gate(*, task, repo, gate, gate_input, context, **_kw):
        # The real standin writes plan_approval.json — emulate that.
        workspace_root = context["workspace_root"]
        path = os.path.join(workspace_root, PLAN_APPROVAL_PATH)
        with open(path, "w") as fh:
            json.dump(
                {
                    "schema_version": "1",
                    "verdict": "approved",
                    "comments": "PO standin approved.",
                    "source": "po",
                    "agent_id": "po:42",
                    "written_at": "2026-05-14T12:00:00Z",
                },
                fh,
            )
        return True

    # design_approval.resume_after_design_approval transitions the task to
    # ARCHITECT_BACKLOG_EMIT on approved verdict. Mirror that on the fake
    # so the post-transition state read inside ``_advance_through_design_gate``
    # sees the updated status and falls through into run_initial.
    from shared.models import TaskStatus

    async def fake_transition_task(task_id, to_status, *_a, **_kw):
        if to_status == "architect_backlog_emit":
            fake_task.status = TaskStatus.ARCHITECT_BACKLOG_EMIT
        elif to_status == "blocked":
            fake_task.status = TaskStatus.BLOCKED

    with (
        patch.object(trio, "async_session", _patched_async_session_factory(fake_task)),
        patch.object(architect_mod, "run_design", run_design_mock),
        patch.object(architect_mod, "run_initial", run_initial_mock),
        patch.object(
            architect_mod,
            "_prepare_parent_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
        patch.object(
            design_approval_mod,
            "transition_task",
            AsyncMock(side_effect=fake_transition_task),
        ),
        patch.object(trio, "_ensure_integration_branch_checked_out", new=AsyncMock()),
        patch.object(trio, "_maybe_dispatch_sub_architects", new=AsyncMock(return_value=False)),
        patch("agent.lifecycle.factory.home_dir_for_task", new=AsyncMock(return_value=None)),
        patch.object(trio, "_drive_final_review_and_pr", drive_final_mock),
        patch.object(trio, "_resolve_target_branch", new=AsyncMock(return_value="main")),
        patch.object(trio, "run_freeform_gate", new=AsyncMock(side_effect=fake_run_freeform_gate)),
    ):
        await trio.run_trio_parent(fake_task)

    # Freeform must NOT call run_design — design.md already exists.
    run_design_mock.assert_not_awaited()
    # The standin's plan_approval.json must drive the flow into run_initial.
    run_initial_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. Human-in-loop + missing approval → return without progress.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_in_loop_missing_approval_returns_without_progress(tmp_path):
    """When the design gate is open but no approval has landed,
    ``run_trio_parent`` must return without calling either architect
    entry point. The orchestrator handler re-enters when the user posts."""

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / DESIGN_PATH).write_text("# Design\n")
    # plan_approval.json deliberately missing.

    fake_task = _make_fake_task(
        complexity="complex_large",
        status="awaiting_design_approval",
        trio_backlog=None,
        freeform_mode=False,
        mode_override="human_in_loop",
    )

    run_design_mock = AsyncMock()
    run_initial_mock = AsyncMock()

    with (
        patch.object(trio, "async_session", _patched_async_session_factory(fake_task)),
        patch.object(architect_mod, "run_design", run_design_mock),
        patch.object(architect_mod, "run_initial", run_initial_mock),
        patch.object(
            architect_mod,
            "_prepare_parent_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
        patch.object(trio, "_ensure_integration_branch_checked_out", new=AsyncMock()),
        patch.object(trio, "_maybe_dispatch_sub_architects", new=AsyncMock(return_value=False)),
        patch("agent.lifecycle.factory.home_dir_for_task", new=AsyncMock(return_value=None)),
    ):
        await trio.run_trio_parent(fake_task)

    run_design_mock.assert_not_awaited()
    run_initial_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Sanity check — non-complex_large trio runs are NOT gated through
# ``run_design`` (the gate is scoped to complex_large per ADR-015 §2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complex_non_large_trio_skips_design_gate(tmp_path):
    """A freeform ``complex`` task is routed through ``run_trio_parent``
    too (run.py:1188). ADR-015 §2 scopes the design gate to complex_large
    only; this test pins that the existing run_initial path is preserved
    for non-complex_large trio runs."""

    fake_task = _make_fake_task(
        complexity="complex",
        status="trio_executing",
        trio_backlog=None,
        freeform_mode=True,
        mode_override="freeform",
    )

    run_design_mock = AsyncMock()
    run_initial_mock = AsyncMock()
    drive_final_mock = AsyncMock()

    with (
        patch.object(trio, "async_session", _patched_async_session_factory(fake_task)),
        patch.object(architect_mod, "run_design", run_design_mock),
        patch.object(architect_mod, "run_initial", run_initial_mock),
        patch.object(
            architect_mod,
            "_prepare_parent_workspace",
            new=AsyncMock(return_value=str(tmp_path)),
        ),
        patch.object(trio, "_ensure_integration_branch_checked_out", new=AsyncMock()),
        patch.object(trio, "_maybe_dispatch_sub_architects", new=AsyncMock(return_value=False)),
        patch("agent.lifecycle.factory.home_dir_for_task", new=AsyncMock(return_value=None)),
        patch.object(trio, "_drive_final_review_and_pr", drive_final_mock),
        patch.object(trio, "_resolve_target_branch", new=AsyncMock(return_value="main")),
    ):
        await trio.run_trio_parent(fake_task)

    run_design_mock.assert_not_awaited()
    run_initial_mock.assert_awaited_once()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
