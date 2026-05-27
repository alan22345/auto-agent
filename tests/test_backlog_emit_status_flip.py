"""After ``architect.run_initial`` writes the backlog, the outer task
status must flip from ``ARCHITECT_BACKLOG_EMIT`` to ``TRIO_EXECUTING``
so:

  1. The UI shows the right outer state during the per-item dispatch loop.
  2. The ``trio_recovery`` startup hook (which only picks
     ``TRIO_EXECUTING`` tasks) can resume the parent on container restart.

Task 30 (2026-05-27) exposed this: the per-item loop was happily
dispatching the first coder while ``status`` still said
``ARCHITECT_BACKLOG_EMIT`` — the state-machine table allowed the
transition (``"backlog emitted → builder dispatch"``), the code
just never called it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_parent(*, status="architect_backlog_emit", has_backlog=False):
    """Stand-in for a Task row hot enough to drive _advance_through_design_gate."""
    from shared.models import TaskComplexity, TaskStatus

    status_enum = TaskStatus(status)
    return SimpleNamespace(
        id=42,
        title="prettier apartment",
        complexity=TaskComplexity.COMPLEX_LARGE,
        status=status_enum,
        trio_backlog=[{"id": "T1"}] if has_backlog else None,
        freeform_mode=False,
        repo_id=1,
        repo=SimpleNamespace(id=1, name="iot", url="https://x", default_branch="main"),
        created_by_user_id=1,
        organization_id=1,
        description="...",
    )


@pytest.mark.asyncio
async def test_backlog_emit_flips_status_to_trio_executing(monkeypatch, tmp_path):
    """The architect's run_initial completes; the gate helper flips the
    outer status to TRIO_EXECUTING."""
    from agent.lifecycle.trio import _advance_through_design_gate
    from shared.models import TaskStatus

    parent = _make_parent(status="architect_backlog_emit")

    # The session-mocking dance below mirrors what the gate actually
    # does: it re-reads the task from DB. We track the status across
    # those re-reads and verify the transition fires.
    db_status = {"value": TaskStatus.ARCHITECT_BACKLOG_EMIT}
    transitions: list[tuple[str, str]] = []

    class _Live:
        def __init__(self, status_enum):
            self.status = status_enum
            self.id = parent.id
            self.complexity = parent.complexity
            self.trio_backlog = parent.trio_backlog

    class _Result:
        def __init__(self, live):
            self._live = live

        def scalar_one(self):
            return self._live

    async def _exec(stmt):
        return _Result(_Live(db_status["value"]))

    async def _commit():
        return None

    async def _aenter(_self=None):
        return cm

    async def _aexit(*args):
        return False

    cm = MagicMock()
    cm.execute = _exec
    cm.commit = _commit
    session_ctx = MagicMock()
    session_ctx.__aenter__ = _aenter
    session_ctx.__aexit__ = _aexit

    def _async_session_factory():
        return session_ctx

    async def fake_transition(session, task, new_status, **kw):
        transitions.append((task.status.value, new_status.value))
        db_status["value"] = new_status

    async def fake_run_initial(task_id):
        # Architect succeeds without changing status — happy path.
        return None

    async def fake_set_trio_phase(task_id, phase):
        return None

    async def fake_prepare(parent_):
        return SimpleNamespace(root=str(tmp_path))

    def fake_design_md_exists(ws, tid):
        return False  # forces the ARCHITECT_BACKLOG_EMIT-status branch

    monkeypatch.setattr(
        "agent.lifecycle.trio.async_session",
        _async_session_factory,
    )
    monkeypatch.setattr("agent.lifecycle.trio.transition", fake_transition)
    monkeypatch.setattr(
        "agent.lifecycle.trio.architect.run_initial",
        fake_run_initial,
    )
    monkeypatch.setattr(
        "agent.lifecycle.trio._set_trio_phase",
        fake_set_trio_phase,
    )
    monkeypatch.setattr(
        "agent.lifecycle.trio.architect._prepare_parent_workspace",
        fake_prepare,
    )
    monkeypatch.setattr(
        "agent.lifecycle.trio._design_md_exists",
        fake_design_md_exists,
    )

    result = await _advance_through_design_gate(parent)

    assert result is True
    assert any(
        from_s == "architect_backlog_emit" and to_s == "trio_executing"
        for from_s, to_s in transitions
    ), f"expected ARCHITECT_BACKLOG_EMIT→TRIO_EXECUTING transition; saw {transitions}"


@pytest.mark.asyncio
async def test_resume_with_existing_backlog_also_flips_status(monkeypatch, tmp_path):
    """Recovery-hook re-entry case: the backlog already exists on disk
    (from a prior run that was interrupted by a deploy), the gate
    early-returns down the "skip run_initial" branch. That branch must
    ALSO flip the outer status — otherwise the recovered task stays
    pinned at ARCHITECT_BACKLOG_EMIT and a second restart wouldn't
    pick it back up (trio_recovery only resumes TRIO_EXECUTING)."""
    from agent.lifecycle.trio import _advance_through_design_gate
    from shared.models import TaskStatus

    parent = _make_parent(status="architect_backlog_emit", has_backlog=True)

    db_status = {"value": TaskStatus.ARCHITECT_BACKLOG_EMIT}
    transitions: list[tuple[str, str]] = []

    class _Live:
        def __init__(self, s):
            self.status = s
            self.id = parent.id
            self.complexity = parent.complexity
            self.trio_backlog = parent.trio_backlog

    class _Result:
        def __init__(self, live):
            self._live = live

        def scalar_one(self):
            return self._live

    async def _exec(stmt):
        return _Result(_Live(db_status["value"]))

    async def _commit():
        return None

    async def _aenter(_self=None):
        return cm

    async def _aexit(*args):
        return False

    cm = MagicMock()
    cm.execute = _exec
    cm.commit = _commit
    session_ctx = MagicMock()
    session_ctx.__aenter__ = _aenter
    session_ctx.__aexit__ = _aexit

    async def fake_transition(session, task, new_status, **kw):
        transitions.append((task.status.value, new_status.value))
        db_status["value"] = new_status

    monkeypatch.setattr("agent.lifecycle.trio.async_session", lambda: session_ctx)
    monkeypatch.setattr("agent.lifecycle.trio.transition", fake_transition)
    monkeypatch.setattr(
        "agent.lifecycle.trio.architect._prepare_parent_workspace",
        AsyncMock(return_value=SimpleNamespace(root=str(tmp_path))),
    )
    # _design_md_exists irrelevant for the has_backlog branch.

    result = await _advance_through_design_gate(parent)

    assert result is True
    assert any(
        from_s == "architect_backlog_emit" and to_s == "trio_executing"
        for from_s, to_s in transitions
    ), f"resume path must also flip; saw {transitions}"


@pytest.mark.asyncio
async def test_backlog_emit_does_not_flip_when_run_initial_blocked(monkeypatch, tmp_path):
    """If run_initial transitioned the task to BLOCKED (invalid JSON),
    the gate must NOT clobber that with a TRIO_EXECUTING flip."""
    from agent.lifecycle.trio import _advance_through_design_gate
    from shared.models import TaskStatus

    parent = _make_parent(status="architect_backlog_emit")

    db_status = {"value": TaskStatus.ARCHITECT_BACKLOG_EMIT}
    transitions: list[tuple[str, str]] = []

    class _Live:
        def __init__(self, status_enum):
            self.status = status_enum
            self.id = parent.id
            self.complexity = parent.complexity
            self.trio_backlog = parent.trio_backlog

    class _Result:
        def __init__(self, live):
            self._live = live

        def scalar_one(self):
            return self._live

    async def _exec(stmt):
        return _Result(_Live(db_status["value"]))

    async def _commit():
        return None

    async def _aenter(_self=None):
        return cm

    async def _aexit(*args):
        return False

    cm = MagicMock()
    cm.execute = _exec
    cm.commit = _commit
    session_ctx = MagicMock()
    session_ctx.__aenter__ = _aenter
    session_ctx.__aexit__ = _aexit

    async def fake_transition(session, task, new_status, **kw):
        transitions.append((task.status.value, new_status.value))
        db_status["value"] = new_status

    async def fake_run_initial(task_id):
        # Simulate run_initial transitioning to BLOCKED on invalid JSON.
        db_status["value"] = TaskStatus.BLOCKED

    monkeypatch.setattr("agent.lifecycle.trio.async_session", lambda: session_ctx)
    monkeypatch.setattr("agent.lifecycle.trio.transition", fake_transition)
    monkeypatch.setattr(
        "agent.lifecycle.trio.architect.run_initial",
        fake_run_initial,
    )
    monkeypatch.setattr(
        "agent.lifecycle.trio._set_trio_phase",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "agent.lifecycle.trio.architect._prepare_parent_workspace",
        AsyncMock(return_value=SimpleNamespace(root=str(tmp_path))),
    )
    monkeypatch.setattr(
        "agent.lifecycle.trio._design_md_exists",
        lambda *a, **kw: False,
    )

    await _advance_through_design_gate(parent)

    # No transition out of BLOCKED — the run_initial branch already
    # owned the final status.
    assert not any(from_s == "blocked" for from_s, _ in transitions), (
        f"must not flip away from BLOCKED; saw {transitions}"
    )
