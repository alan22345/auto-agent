"""Sub-architect dispatch — ADR-015 §9 / Phase 8.

When the parent architect emits ``decision.action == "spawn_sub_architects"``,
the orchestrator dispatches sub-architects serially (parent's session
must stay alive to answer grill questions, see §10). Each slice runs in
its own ``.auto-agent/slices/<name>/`` namespace.

Pins:

1. Two-slice spawn dispatches sequentially — not asyncio.gather'd.
2. Each slice writes its own ``slices/<name>/design.md`` + ``backlog.json``.
3. All slices complete → result reports success → parent transitions
   to FINAL_REVIEW (caller responsibility; dispatcher returns the
   aggregated outcome).
4. One slice's permanent failure → aggregated result marks blocked.
5. Sub-architect that itself emits ``spawn_sub_architects`` is rejected
   (1-level recursion bound).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Test helpers — a fake "sub-architect runner" that we can patch in.
# ---------------------------------------------------------------------------


def _make_fake_runner(
    workspace_root: Path,
    *,
    fail_slice: str | None = None,
    nested_spawn_slice: str | None = None,
) -> tuple[Any, list[str]]:
    """Return ``(runner, order_log)``.

    The runner records the order in which slices were invoked (verifies
    serial execution). For each slice it writes
    ``slices/<name>/design.md`` and ``slices/<name>/backlog.json`` to
    simulate the real sub-architect's skill writes. If the slice name
    matches ``fail_slice`` the runner returns a failure outcome. If it
    matches ``nested_spawn_slice`` the runner writes a slice-scoped
    decision.json with action ``spawn_sub_architects`` (which the
    dispatcher must reject per the 1-level bound).
    """

    order: list[str] = []

    async def runner(
        *,
        parent_task,
        slice_spec,
        workspace_root,
        **_kw,
    ) -> dict[str, Any]:
        name = slice_spec["name"]
        order.append(name)

        slice_root = Path(workspace_root) / ".auto-agent" / "slices" / name
        slice_root.mkdir(parents=True, exist_ok=True)
        (slice_root / "design.md").write_text(
            f"# Slice {name}\n\nScope: {slice_spec.get('scope', '')}\n"
        )
        (slice_root / "backlog.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "items": [
                        {
                            "id": f"{name}-T1",
                            "title": f"{name} item",
                            "description": "x" * 100,
                            "justification": "needed",
                            "affected_routes": [],
                            "affected_files_estimate": 1,
                        }
                    ],
                }
            )
        )

        if nested_spawn_slice == name:
            (slice_root / "decision.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "action": "spawn_sub_architects",
                        "payload": {"slices": [{"name": "nested", "scope": "should be rejected"}]},
                    }
                )
            )

        if fail_slice == name:
            return {"status": "failed", "reason": f"forced fail for {name}"}
        return {"status": "completed"}

    return runner, order


def _parent_stub(parent_id: int = 99):
    """Return a lightweight stub with the fields ``dispatch_sub_architects``
    reads — id, repo, organization_id, created_by_user_id."""

    class _Parent:
        id = parent_id
        title = "build a TODO app"
        description = "Build TODO"
        repo = None
        organization_id = 1
        created_by_user_id = 1

    return _Parent()


# ---------------------------------------------------------------------------
# 1. Serial dispatch (NOT parallel).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_slices_dispatched_sequentially(tmp_path: Path) -> None:
    """The dispatcher walks slices in declared order, one at a time.

    Serial execution is structurally required: the parent's session
    needs to be alive to answer grill questions (§10). asyncio.gather
    would let two sub-architects race to grill the parent at once.
    """
    from agent.lifecycle.trio import sub_architect

    runner, order = _make_fake_runner(tmp_path)
    slices = [
        {"name": "auth", "scope": "auth subsystem"},
        {"name": "checkout", "scope": "checkout subsystem"},
    ]

    with patch.object(sub_architect, "_run_sub_architect_slice", runner):
        result = await sub_architect.dispatch_sub_architects(
            parent_task=_parent_stub(),
            workspace_root=str(tmp_path),
            slices=slices,
        )

    assert order == ["auth", "checkout"]
    assert result.ok is True
    assert {s.name for s in result.slice_results} == {"auth", "checkout"}
    assert all(s.status == "completed" for s in result.slice_results)


# ---------------------------------------------------------------------------
# 2. Each slice gets its own design.md + backlog.json.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_slice_writes_its_own_design_and_backlog(tmp_path: Path) -> None:
    from agent.lifecycle.trio import sub_architect
    from agent.lifecycle.workspace_paths import slice_backlog_path, slice_design_path

    runner, _ = _make_fake_runner(tmp_path)
    slices = [
        {"name": "auth", "scope": "auth"},
        {"name": "checkout", "scope": "checkout"},
    ]
    with patch.object(sub_architect, "_run_sub_architect_slice", runner):
        await sub_architect.dispatch_sub_architects(
            parent_task=_parent_stub(),
            workspace_root=str(tmp_path),
            slices=slices,
        )

    for name in ("auth", "checkout"):
        assert (tmp_path / slice_design_path(name)).is_file()
        assert (tmp_path / slice_backlog_path(name)).is_file()
    # Namespace isolation — auth's design.md does not appear in checkout's namespace.
    assert "Scope: auth" in (tmp_path / slice_design_path("auth")).read_text()
    assert "Scope: checkout" in (tmp_path / slice_design_path("checkout")).read_text()


# ---------------------------------------------------------------------------
# 3. All slices complete → aggregated ok=True.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_slices_complete_aggregates_ok(tmp_path: Path) -> None:
    from agent.lifecycle.trio import sub_architect

    runner, _ = _make_fake_runner(tmp_path)
    slices = [
        {"name": "a", "scope": "x"},
        {"name": "b", "scope": "y"},
        {"name": "c", "scope": "z"},
    ]
    with patch.object(sub_architect, "_run_sub_architect_slice", runner):
        result = await sub_architect.dispatch_sub_architects(
            parent_task=_parent_stub(),
            workspace_root=str(tmp_path),
            slices=slices,
        )
    assert result.ok is True
    assert result.blocked_reason is None


# ---------------------------------------------------------------------------
# 4. One slice fails permanently → aggregated result marks blocked.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_slice_permanent_failure_blocks(tmp_path: Path) -> None:
    """When a slice returns ``status=failed`` the aggregated result is
    not ok and carries a reason the caller can persist on the parent."""
    from agent.lifecycle.trio import sub_architect

    runner, order = _make_fake_runner(tmp_path, fail_slice="checkout")
    slices = [
        {"name": "auth", "scope": "auth"},
        {"name": "checkout", "scope": "checkout"},
        {"name": "search", "scope": "search"},
    ]
    with patch.object(sub_architect, "_run_sub_architect_slice", runner):
        result = await sub_architect.dispatch_sub_architects(
            parent_task=_parent_stub(),
            workspace_root=str(tmp_path),
            slices=slices,
        )

    assert result.ok is False
    assert result.blocked_reason
    assert "checkout" in result.blocked_reason
    # Serial execution short-circuits: once a slice fails permanently,
    # the third slice should not be invoked.
    assert order == ["auth", "checkout"], "subsequent slices must not run after a permanent failure"


# ---------------------------------------------------------------------------
# 5. 1-level recursion bound — sub-architect emitting spawn_sub_architects
#    is rejected and that slice fails.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_architect_cannot_spawn_sub_sub_architects(tmp_path: Path) -> None:
    """A sub-architect that emits ``spawn_sub_architects`` in its own
    slice-scoped decision.json must be rejected: dispatcher records a
    permanent failure for that slice with a reason mentioning the bound.
    """
    from agent.lifecycle.trio import sub_architect

    runner, _ = _make_fake_runner(tmp_path, nested_spawn_slice="auth")
    slices = [{"name": "auth", "scope": "auth"}]
    with patch.object(sub_architect, "_run_sub_architect_slice", runner):
        result = await sub_architect.dispatch_sub_architects(
            parent_task=_parent_stub(),
            workspace_root=str(tmp_path),
            slices=slices,
        )

    auth = next(s for s in result.slice_results if s.name == "auth")
    # Either the runner returned completed but the dispatcher rejects
    # post-hoc; or the dispatcher synthesises a failure. Either way the
    # final per-slice status must be a failure with a recursion reason.
    assert auth.status == "failed"
    assert "sub-architect" in (auth.reason or "").lower()
    assert (
        "spawn" in (auth.reason or "").lower()
        or "recursion" in (auth.reason or "").lower()
        or "1-level" in (auth.reason or "")
    )
    assert result.ok is False


# ---------------------------------------------------------------------------
# 6. Validator rejects spawn_sub_architects when called from a sub-architect
#    context — load-bearing for the recursion bound enforcement.
# ---------------------------------------------------------------------------


def test_validator_rejects_nested_spawn(tmp_path: Path) -> None:
    """The decision-shape validator rejects ``spawn_sub_architects`` when
    the architect is itself a sub-architect (``is_sub_architect=True``).

    This is the structural enforcement point — the dispatcher does the
    runtime rejection, but the validator is the contract.
    """
    from agent.lifecycle.trio import sub_architect

    # Parent context: spawn_sub_architects is valid.
    ok, _ = sub_architect.validate_decision_for_role(
        decision={
            "action": "spawn_sub_architects",
            "payload": {"slices": [{"name": "a", "scope": "a"}]},
        },
        is_sub_architect=False,
    )
    assert ok is True

    # Sub-architect context: spawn_sub_architects is rejected.
    ok, reason = sub_architect.validate_decision_for_role(
        decision={
            "action": "spawn_sub_architects",
            "payload": {"slices": [{"name": "deeper", "scope": "x"}]},
        },
        is_sub_architect=True,
    )
    assert ok is False
    assert reason and "sub-architect" in reason.lower()


# ---------------------------------------------------------------------------
# 7. State machine: AWAITING_SUB_ARCHITECTS transitions exist.
# ---------------------------------------------------------------------------


def test_state_machine_has_awaiting_sub_architects_transitions() -> None:
    """ADR-015 §9 / Phase 8:

    ARCHITECT_BACKLOG_EMIT → AWAITING_SUB_ARCHITECTS   (spawn emitted)
    AWAITING_SUB_ARCHITECTS → FINAL_REVIEW              (all slices done)
    AWAITING_SUB_ARCHITECTS → BLOCKED                   (a slice failed)
    """
    from orchestrator.state_machine import TRANSITIONS
    from shared.models import TaskStatus

    assert hasattr(TaskStatus, "AWAITING_SUB_ARCHITECTS")
    awaiting = TaskStatus.AWAITING_SUB_ARCHITECTS

    # Entry: from ARCHITECT_BACKLOG_EMIT when architect emits spawn.
    assert awaiting in TRANSITIONS[TaskStatus.ARCHITECT_BACKLOG_EMIT]

    # Exits: FINAL_REVIEW on success, BLOCKED on slice failure.
    allowed = TRANSITIONS[awaiting]
    assert TaskStatus.FINAL_REVIEW in allowed
    assert TaskStatus.BLOCKED in allowed


# ---------------------------------------------------------------------------
# 8. Dispatcher invokes the runner with the workspace_root joined to the
#    slice subdir (slice-scoped cwd, not the root .auto-agent/).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_passes_slice_subdir_to_runner(tmp_path: Path) -> None:
    """The runner must see a slice_root path under ``.auto-agent/slices/<name>/``
    so its session, decision file, and review files live there."""
    from agent.lifecycle.trio import sub_architect

    captured: list[dict] = []

    async def runner(*, parent_task, slice_spec, workspace_root, slice_root, **_kw):
        captured.append({"slice_root": slice_root, "name": slice_spec["name"]})
        Path(slice_root).mkdir(parents=True, exist_ok=True)
        return {"status": "completed"}

    slices = [{"name": "auth", "scope": "auth"}]
    with patch.object(sub_architect, "_run_sub_architect_slice", runner):
        await sub_architect.dispatch_sub_architects(
            parent_task=_parent_stub(),
            workspace_root=str(tmp_path),
            slices=slices,
        )

    assert len(captured) == 1
    expected = tmp_path / ".auto-agent" / "slices" / "auth"
    assert Path(captured[0]["slice_root"]) == expected


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
