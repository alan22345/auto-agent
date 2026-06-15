"""A zero-child domain dispatch must not deadlock the scaffold parent.

Incident (2026-06-14, scaffold #329 ant-simulator): after all 7 domains
built+merged, Phase E final verification found a (likely spurious) gap and
re-entered Phase D to dispatch fix children. ``dispatch_children.run``
reads the domain list from the parent workspace's root ADR
(``.auto-agent/adrs/000-system.md``) — a gitignored, workspace-only
artifact. The artifact had been lost when the workspace was recreated, so
``run`` logged ``root_adr_missing`` and returned ``[]`` (zero children).

The parent IGNORED the empty return, transitioned to BUILDING_DOMAINS, and
waited for the on-task-finished fan-in — which only fires when a child
reaches a terminal state. No new child existed and all old children were
already terminal, so the event never fired: permanent deadlock.

Fix: when Phase D dispatches zero new children there is nothing to wait
for, so re-enter final verification (bounded by MAX_FINAL_VERIFY_ROUNDS)
instead of parking in BUILDING_DOMAINS.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle.scaffold import parent as parent_mod


def _make_task(status: str):
    from shared.models import TaskComplexity, TaskSource, TaskStatus

    return SimpleNamespace(
        id=329,
        title="Scaffold: ant-simulator",
        description="Build an ant simulator.",
        status=TaskStatus(status),
        complexity=TaskComplexity.SCAFFOLD,
        repo_id=301,
        repo=None,
        freeform_mode=True,
        organization_id=1,
        created_by_user_id=None,
        parent_task_id=None,
        subtasks=None,
        source=TaskSource.MANUAL,
        affected_routes=[],
    )


@asynccontextmanager
async def _round_counter_session():
    class _Result:
        def scalar_one(self):
            return SimpleNamespace(subtasks=None)

    class _Session:
        async def execute(self, *_a, **_kw):
            return _Result()

        async def commit(self):
            pass

        def add(self, _obj):
            pass

    yield _Session()


@pytest.mark.asyncio
async def test_empty_dispatch_reenters_final_verification_not_building_domains():
    from shared.models import TaskStatus

    task = _make_task("dispatching_domain_builds")
    transitions: list[str] = []

    async def fake_transition_and_reload(task_id, to_status, *, message=""):
        transitions.append(to_status.value)
        return SimpleNamespace(
            id=task.id, status=to_status, complexity=task.complexity,
            subtasks=task.subtasks, title=task.title, description=task.description,
            repo_id=task.repo_id, repo=task.repo, freeform_mode=task.freeform_mode,
            organization_id=task.organization_id, created_by_user_id=task.created_by_user_id,
            parent_task_id=task.parent_task_id, source=task.source,
            affected_routes=task.affected_routes,
        )

    with (
        patch.object(parent_mod.dispatch_children, "run", AsyncMock(return_value=[])),
        patch.object(parent_mod.final_verification, "run", AsyncMock(return_value="passed")),
        patch.object(parent_mod, "_transition_and_reload", fake_transition_and_reload),
        patch.object(parent_mod, "async_session", _round_counter_session),
    ):
        await parent_mod.run_scaffold_parent(task)

    assert TaskStatus.BUILDING_DOMAINS.value not in transitions, (
        "zero-child dispatch must NOT park in BUILDING_DOMAINS (deadlock)"
    )
    assert TaskStatus.AWAITING_FINAL_VERIFICATION.value in transitions
    # And with a passing re-verification it should reach DONE.
    assert TaskStatus.DONE.value in transitions


@pytest.mark.asyncio
async def test_nonempty_dispatch_still_waits_in_building_domains():
    """The happy path is unchanged: real children → park in BUILDING_DOMAINS."""
    from shared.models import TaskStatus

    task = _make_task("dispatching_domain_builds")
    transitions: list[str] = []

    async def fake_transition_and_reload(task_id, to_status, *, message=""):
        transitions.append(to_status.value)
        return SimpleNamespace(
            id=task.id, status=to_status, complexity=task.complexity,
            subtasks=task.subtasks, title=task.title, description=task.description,
            repo_id=task.repo_id, repo=task.repo, freeform_mode=task.freeform_mode,
            organization_id=task.organization_id, created_by_user_id=task.created_by_user_id,
            parent_task_id=task.parent_task_id, source=task.source,
            affected_routes=task.affected_routes,
        )

    with (
        patch.object(parent_mod.dispatch_children, "run", AsyncMock(return_value=[330, 331])),
        patch.object(parent_mod.final_verification, "run", AsyncMock(return_value="passed")),
        patch.object(parent_mod, "_transition_and_reload", fake_transition_and_reload),
        patch.object(parent_mod, "async_session", _round_counter_session),
    ):
        await parent_mod.run_scaffold_parent(task)

    assert transitions == [TaskStatus.BUILDING_DOMAINS.value]
