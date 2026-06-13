"""Gap-fix dispatcher must retry on a malformed/empty architect decision.

Incident (2026-06-13, scaffold #329 ant-simulator): child domain-build
#332 (pheromone-field) went BLOCKED on the FIRST gap-fix round because
the gap-fix architect wrote a ``decision.json`` with no valid ``action``
(``action=None``, empty reason). The dispatcher's catch-all treated any
non-``dispatch_new`` action as terminal and blocked the parent — burning
the 3-round gap-fix budget on a single transient agent contract
violation, which jammed the serial scaffold chain (siblings #333-336
stuck in intake behind it).

A malformed/empty decision is a transient failure, not a deliberate
architect choice, so the dispatcher must RETRY the round (bounded by
MAX_GAP_FIX_ROUNDS). A deliberate ``escalate`` still blocks immediately.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import agent.lifecycle.trio as trio
from agent.lifecycle.trio import final_reviewer as final_reviewer_mod
from agent.lifecycle.trio import gap_fix as gap_fix_mod


def _patched_async_session_factory(fake_task):
    @asynccontextmanager
    async def factory():
        class _ExecResult:
            def scalar_one(self):
                return fake_task

            def scalar_one_or_none(self):
                return fake_task

        class _Session:
            async def execute(self, *_a, **_kw):
                return _ExecResult()

            def add(self, _obj):
                pass

            async def commit(self):
                pass

            async def flush(self):
                pass

        yield _Session()

    return factory


def _fake_parent():
    from shared.models import TaskStatus

    return SimpleNamespace(
        id=332,
        status=TaskStatus.TRIO_EXECUTING,
        trio_phase=None,
        repo_id=301,
    )


def _gaps_found():
    return SimpleNamespace(verdict="gaps_found", gaps=[{"description": "g", "affected_routes": []}])


async def _drive(parent, *, run_gap_fix_mock, block_mock):
    with (
        patch.object(trio, "async_session", _patched_async_session_factory(parent)),
        patch.object(
            final_reviewer_mod,
            "run_final_review",
            AsyncMock(return_value=_gaps_found()),
        ) as review_mock,
        patch.object(gap_fix_mod, "run_gap_fix", run_gap_fix_mock),
        patch.object(trio, "_block_parent", block_mock),
        patch.object(trio, "_open_integration_pr_and_transition", new=AsyncMock()),
        patch.object(trio, "_append_backlog_items", new=AsyncMock()),
        patch.object(trio, "run_trio_parent", new=AsyncMock()),
    ):
        await trio._drive_final_review_and_pr(
            parent=parent,
            workspace_root="/tmp/ws",
            repo_name=None,
            home_dir=None,
            org_id=1,
            target_branch="main",
        )
    return review_mock


@pytest.mark.asyncio
async def test_malformed_decision_retries_then_blocks_after_budget():
    """action=None must retry every round, only blocking once the budget is spent."""

    run_gap_fix_mock = AsyncMock(return_value={"action": None, "reason": ""})
    block_mock = AsyncMock()

    review_mock = await _drive(
        _fake_parent(), run_gap_fix_mock=run_gap_fix_mock, block_mock=block_mock
    )

    # Retried every round instead of blocking on round 1.
    assert run_gap_fix_mock.await_count == gap_fix_mod.MAX_GAP_FIX_ROUNDS
    # One extra review for the final (budget-exhausted) round.
    assert review_mock.await_count == gap_fix_mod.MAX_GAP_FIX_ROUNDS + 1
    # Blocked exactly once, with the budget-exhaustion message — NOT the
    # round-1 "emitted action=None" terminal path.
    block_mock.assert_awaited_once()
    msg = block_mock.await_args.args[1]
    assert "gap-fix rounds" in msg
    assert "action=None" not in msg


@pytest.mark.asyncio
async def test_deliberate_escalate_blocks_immediately():
    """A real ``escalate`` is an architect decision and still blocks on round 1."""

    run_gap_fix_mock = AsyncMock(
        return_value={"action": "escalate", "reason": "gap is out-of-scope"}
    )
    block_mock = AsyncMock()

    await _drive(_fake_parent(), run_gap_fix_mock=run_gap_fix_mock, block_mock=block_mock)

    assert run_gap_fix_mock.await_count == 1
    block_mock.assert_awaited_once()
    msg = block_mock.await_args.args[1]
    assert "escalat" in msg.lower()
