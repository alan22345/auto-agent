"""Design-doc approval gate for complex_large — ADR-015 §2 / Phase 6.

The architect's initial run no longer emits a backlog directly. The
first turn writes ``.auto-agent/design.md`` via the ``submit-design``
skill; the parent task transitions to ``AWAITING_DESIGN_APPROVAL`` and
waits for ``.auto-agent/plan_approval.json``. Approval transitions the
parent to ``ARCHITECT_BACKLOG_EMIT``; rejection transitions to
``BLOCKED`` with the comments attached.

Five behaviours pinned here:

1. ``write_design`` writes ``.auto-agent/design.md`` to the workspace.
2. ``finalize_design`` writes the file AND transitions the parent task
   to ``AWAITING_DESIGN_APPROVAL``.
3. ``approved`` verdict → ARCHITECT_BACKLOG_EMIT.
4. ``rejected`` verdict → BLOCKED with comments visible in the message.
5. Missing approval file ⇒ no transition (orchestrator polls).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# 1. write_design writes .auto-agent/design.md.
# ---------------------------------------------------------------------------


def test_write_design_creates_design_md(tmp_path: Path) -> None:
    from agent.lifecycle.trio.design_approval import write_design
    from agent.lifecycle.workspace_paths import DESIGN_PATH

    design_text = "# Design\n\nGoal: build a TODO app.\n"
    write_design(str(tmp_path), design_text)

    target = tmp_path / DESIGN_PATH
    assert target.is_file()
    assert target.read_text() == design_text


def test_write_design_creates_auto_agent_dir(tmp_path: Path) -> None:
    from agent.lifecycle.trio.design_approval import write_design

    assert not (tmp_path / ".auto-agent").exists()
    write_design(str(tmp_path), "# d\n")
    assert (tmp_path / ".auto-agent").is_dir()


# ---------------------------------------------------------------------------
# 2. finalize_design transitions to AWAITING_DESIGN_APPROVAL.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_design_writes_and_transitions(tmp_path: Path) -> None:
    from agent.lifecycle.trio import design_approval
    from agent.lifecycle.workspace_paths import DESIGN_PATH

    transition_mock = AsyncMock()
    with patch.object(design_approval, "transition_task", transition_mock):
        await design_approval.finalize_design(
            task_id=42,
            workspace=str(tmp_path),
            design_text="# Design\n",
        )

    assert (tmp_path / DESIGN_PATH).is_file()
    transition_mock.assert_awaited_once()
    args, _ = transition_mock.call_args
    assert args[0] == 42
    assert args[1] == "awaiting_design_approval"


# ---------------------------------------------------------------------------
# 3. Approved verdict → ARCHITECT_BACKLOG_EMIT.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approved_verdict_resumes_to_backlog_emit(tmp_path: Path) -> None:
    from agent.lifecycle.trio import design_approval
    from agent.lifecycle.workspace_paths import PLAN_APPROVAL_PATH

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / PLAN_APPROVAL_PATH).write_text(
        json.dumps(
            {"schema_version": "1", "verdict": "approved", "comments": ""},
        )
    )

    transition_mock = AsyncMock()
    with patch.object(design_approval, "transition_task", transition_mock):
        advanced = await design_approval.resume_after_design_approval(
            task_id=51,
            workspace=str(tmp_path),
        )

    assert advanced is True
    transition_mock.assert_awaited_once()
    args, _ = transition_mock.call_args
    assert args[0] == 51
    assert args[1] == "architect_backlog_emit"


# ---------------------------------------------------------------------------
# 4. Rejected verdict → BLOCKED with comments.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejected_verdict_transitions_to_blocked(tmp_path: Path) -> None:
    from agent.lifecycle.trio import design_approval
    from agent.lifecycle.workspace_paths import PLAN_APPROVAL_PATH

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / PLAN_APPROVAL_PATH).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "verdict": "rejected",
                "comments": "Stack choice is wrong.",
            },
        )
    )

    transition_mock = AsyncMock()
    with patch.object(design_approval, "transition_task", transition_mock):
        advanced = await design_approval.resume_after_design_approval(
            task_id=52,
            workspace=str(tmp_path),
        )

    assert advanced is True
    transition_mock.assert_awaited_once()
    args, _ = transition_mock.call_args
    assert args[0] == 52
    assert args[1] == "blocked"
    assert "Stack choice is wrong" in args[2]


# ---------------------------------------------------------------------------
# 5. Missing approval file → no transition.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_approval_keeps_task_awaiting(tmp_path: Path) -> None:
    from agent.lifecycle.trio import design_approval

    transition_mock = AsyncMock()
    with patch.object(design_approval, "transition_task", transition_mock):
        advanced = await design_approval.resume_after_design_approval(
            task_id=53,
            workspace=str(tmp_path),
        )

    assert advanced is False
    transition_mock.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_schema_version_raises(tmp_path: Path) -> None:
    from agent.lifecycle.trio import design_approval
    from agent.lifecycle.workspace_paths import PLAN_APPROVAL_PATH

    (tmp_path / ".auto-agent").mkdir()
    (tmp_path / PLAN_APPROVAL_PATH).write_text(
        json.dumps(
            {"schema_version": "999", "verdict": "approved", "comments": ""},
        )
    )

    with pytest.raises(ValueError):
        await design_approval.resume_after_design_approval(
            task_id=54,
            workspace=str(tmp_path),
        )


# ---------------------------------------------------------------------------
# 6. New TaskStatus values present in the state machine.
# ---------------------------------------------------------------------------


def test_state_machine_has_design_gate_transitions() -> None:
    """The full chain must be present:

    TRIO_EXECUTING → ARCHITECT_DESIGNING → AWAITING_DESIGN_APPROVAL →
    ARCHITECT_BACKLOG_EMIT → TRIO_EXECUTING (and BLOCKED on reject).
    """

    from orchestrator.state_machine import TRANSITIONS
    from shared.models import TaskStatus

    for name in (
        "ARCHITECT_DESIGNING",
        "AWAITING_DESIGN_APPROVAL",
        "ARCHITECT_BACKLOG_EMIT",
    ):
        assert hasattr(TaskStatus, name), f"missing TaskStatus.{name}"

    designing = TaskStatus.ARCHITECT_DESIGNING
    awaiting = TaskStatus.AWAITING_DESIGN_APPROVAL
    emit = TaskStatus.ARCHITECT_BACKLOG_EMIT

    # TRIO_EXECUTING can fan out to designing (initial entry).
    assert designing in TRANSITIONS[TaskStatus.TRIO_EXECUTING]
    # designing → awaiting
    assert awaiting in TRANSITIONS[designing]
    # awaiting → emit (approved) and → blocked (rejected)
    allowed = TRANSITIONS[awaiting]
    assert emit in allowed
    assert TaskStatus.BLOCKED in allowed
    # emit → TRIO_EXECUTING (continue with builder dispatch)
    assert TaskStatus.TRIO_EXECUTING in TRANSITIONS[emit]


# ---------------------------------------------------------------------------
# 7. run_initial wires through the design gate (DB-light test).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_initial_writes_design_md_via_skill(tmp_path: Path) -> None:
    """After Phase 6 the architect's first turn is the design pass.

    The agent must write ``.auto-agent/design.md`` (the test simulates the
    skill by having the mocked agent.run() create the file directly). The
    architect entry point reads the file and finalizes the design gate —
    i.e. transitions to AWAITING_DESIGN_APPROVAL — before any backlog is
    emitted.
    """

    from agent.lifecycle.trio import architect as architect_mod
    from agent.lifecycle.workspace_paths import DESIGN_PATH

    workspace = tmp_path

    # The fake agent simulates the skill by creating design.md on .run().
    async def fake_run(*args, **kwargs):
        from unittest.mock import MagicMock

        (workspace / ".auto-agent").mkdir(exist_ok=True)
        (workspace / DESIGN_PATH).write_text("# Design\n\nbuild a TODO app\n")
        return MagicMock(output="Design ready.", tool_calls=[])

    from unittest.mock import MagicMock

    fake_agent = MagicMock()
    fake_agent.run = fake_run
    fake_agent.messages = []
    fake_agent.api_messages = []
    fake_agent.tool_call_log = []

    finalize_mock = AsyncMock()
    save_session_mock = AsyncMock(return_value="trio-99.json")
    journal_mock = MagicMock(return_value=1)

    # Stub the async_session() context manager so the ArchitectAttempt
    # write doesn't actually touch the DB — this test only verifies the
    # design-gate plumbing.
    from contextlib import asynccontextmanager

    fake_db_session = MagicMock()
    fake_db_session.add = MagicMock()
    fake_db_session.commit = AsyncMock()

    @asynccontextmanager
    async def fake_async_session():
        yield fake_db_session

    with (
        patch.object(architect_mod, "create_architect_agent", return_value=fake_agent),
        patch.object(
            architect_mod,
            "_prepare_parent_workspace",
            new=AsyncMock(return_value=str(workspace)),
        ),
        patch.object(
            architect_mod,
            "_load_parent_for_run",
            new=AsyncMock(
                return_value={
                    "task_description": "Build a TODO app",
                    "task_title": "TODO",
                    "repo_name": None,
                    "org_id": 1,
                    "home_dir": None,
                },
            ),
        ),
        patch.object(
            architect_mod,
            "_persist_architect_session",
            new=save_session_mock,
        ),
        patch.object(architect_mod, "async_session", new=fake_async_session),
        patch.object(architect_mod, "append_journal_entry", new=journal_mock),
        patch.object(
            architect_mod,
            "finalize_design",
            new=finalize_mock,
        ),
    ):
        await architect_mod.run_design(parent_task_id=99)

    # The design.md file is on disk.
    assert (workspace / DESIGN_PATH).is_file()
    finalize_mock.assert_awaited_once()
    args, kwargs = finalize_mock.call_args
    assert kwargs.get("task_id", args[0] if args else None) == 99


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
