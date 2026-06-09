"""Regression — independent-reviewer fail-closed runtime gate.

The independent reviewer (trio-child / legacy path) historically treated a
``ui_check`` verdict of ``SKIPPED`` as acceptable: ``approved = code == "OK"
and ui in ("OK", "SKIPPED")``. That is fail-open — a change that the
reviewer never actually ran (no ``affected_routes`` declared, nothing to
screenshot) sailed through with zero runtime verification.

The smart-escalation fix: when ``ui_check`` resolves to ``SKIPPED`` the
reviewer escalates to the smoke agent (boot / test suite / build /
typecheck). A non-pass smoke verdict downgrades the ui-check to a hard
fail so the task loops back instead of merging unverified code. A passing
smoke verdict lets the SKIPPED ui-check approve as before.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.lifecycle import review
from agent.lifecycle.trio.smoke_agent import SmokeAgentResult


def _wire_common(monkeypatch, *, review_output: str, cycles):
    """Patch the DB/clone/agent surface the independent reviewer touches.

    ``cycles`` is the side-effect list for ``_next_review_cycle`` — its
    length controls how many review invocations are set up.
    """

    task = MagicMock(
        id=7001,
        title="refactor helper",
        description="d",
        repo_name="r",
        repo_id=42,
        freeform_mode=False,
        created_by_user_id=None,
        organization_id=1,
        affected_routes=[],  # nothing declared ⇒ ui_check is SKIPPED
    )
    monkeypatch.setattr("agent.lifecycle.review.get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(
        "agent.lifecycle.review.get_repo",
        AsyncMock(return_value=MagicMock(default_branch="main", url="https://github.com/x/y")),
    )
    monkeypatch.setattr("agent.lifecycle.review.clone_repo", AsyncMock(return_value="/tmp/ws"))
    monkeypatch.setattr(
        "agent.lifecycle.review.sh.run",
        AsyncMock(return_value=MagicMock(failed=False, stdout="some diff", stderr="")),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review.home_dir_for_task", AsyncMock(return_value="/tmp/home")
    )
    monkeypatch.setattr(
        "agent.lifecycle.review._next_review_cycle", AsyncMock(side_effect=list(cycles))
    )
    monkeypatch.setattr(
        "agent.lifecycle.review._create_review_attempt",
        AsyncMock(side_effect=[MagicMock(id=i + 1, cycle=c) for i, c in enumerate(cycles)]),
    )
    monkeypatch.setattr("agent.lifecycle.review._update_review_attempt", AsyncMock())
    monkeypatch.setattr("agent.lifecycle.review._review_loop_back", AsyncMock())

    fake_agent = MagicMock(
        run=AsyncMock(return_value=MagicMock(output=review_output, tool_calls=[]))
    )
    monkeypatch.setattr(
        "agent.lifecycle.review.create_agent", MagicMock(return_value=fake_agent)
    )

    publish_calls: list = []

    async def fake_publish(event):
        publish_calls.append(event)

    monkeypatch.setattr("agent.lifecycle.review.publish", fake_publish)

    transitions: list[tuple] = []

    async def fake_transition(task_id, status, message=""):
        transitions.append((task_id, status, message))

    monkeypatch.setattr("agent.lifecycle.review.transition_task", fake_transition)
    return publish_calls, transitions


_UI_SKIPPED = (
    '{"code_review": {"verdict": "OK", "reasoning": "reads fine"}, '
    '"ui_check": {"verdict": "SKIPPED", "reasoning": "no routes to check"}}'
)


@pytest.mark.asyncio
async def test_skipped_ui_check_escalates_to_smoke_and_blocks_on_fail(monkeypatch):
    """code OK + ui SKIPPED + failing smoke ⇒ NOT approved (loops back)."""

    publish_calls, _ = _wire_common(monkeypatch, review_output=_UI_SKIPPED, cycles=[1])

    smoke = AsyncMock(
        return_value=SmokeAgentResult(
            verdict="fail",
            summary="pytest: 1 failed",
            failures=["tests/test_helper.py::test_x failed"],
        )
    )
    monkeypatch.setattr("agent.lifecycle.review.run_smoke_agent", smoke)

    await review.handle_independent_review(7001, "http://pr", "b")

    smoke.assert_awaited_once()
    approved = [e for e in publish_calls if getattr(e, "payload", {}).get("approved") is True]
    assert approved == [], "a failing smoke escalation must not approve"


@pytest.mark.asyncio
async def test_skipped_ui_check_with_passing_smoke_approves(monkeypatch):
    """code OK + ui SKIPPED + passing smoke ⇒ approved (SKIPPED is fine
    once runtime verification actually ran and passed)."""

    publish_calls, _ = _wire_common(monkeypatch, review_output=_UI_SKIPPED, cycles=[1])

    smoke = AsyncMock(return_value=SmokeAgentResult(verdict="pass", summary="pytest green"))
    monkeypatch.setattr("agent.lifecycle.review.run_smoke_agent", smoke)

    await review.handle_independent_review(7001, "http://pr", "b")

    smoke.assert_awaited_once()
    approved = [e for e in publish_calls if getattr(e, "payload", {}).get("approved") is True]
    assert len(approved) == 1, "a passing smoke escalation should approve the SKIPPED ui-check"
