"""Regression — review UI layer.

A change whose declared route renders incorrectly must NOT reach DONE. The
reviewer agent emits a structured combined verdict with ``ui_check=NOT-OK``;
two consecutive review failures land the task in BLOCKED, never publishing
``task_review_complete(approved=True)``.

Maps to acceptance criterion #2 in the spec.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from agent.lifecycle import review


async def test_ui_review_failure_blocks_with_no_approval(monkeypatch):
    task = MagicMock(
        id=9003,
        title="render broken page",
        description="d",
        repo_name="r",
        freeform_mode=True,
        created_by_user_id=None,
        organization_id=1,
        affected_routes=[{"method": "GET", "path": "/broken", "label": "broken"}],
    )

    monkeypatch.setattr(
        "agent.lifecycle.review.get_task", AsyncMock(return_value=task),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review.get_repo",
        AsyncMock(return_value=MagicMock(
            default_branch="main", url="https://github.com/x/y",
        )),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review.get_freeform_config",
        AsyncMock(return_value=MagicMock(
            dev_branch="dev", prod_branch="main", run_command=None,
        )),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review.clone_repo",
        AsyncMock(return_value="/tmp/ws"),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review.sh.run",
        AsyncMock(return_value=MagicMock(failed=False, stdout="", stderr="")),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review.home_dir_for_task",
        AsyncMock(return_value="/tmp/home"),
    )

    # Successive review calls run cycle 1 then cycle 2.
    monkeypatch.setattr(
        "agent.lifecycle.review._next_review_cycle",
        AsyncMock(side_effect=[1, 2]),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review._create_review_attempt",
        AsyncMock(side_effect=[MagicMock(id=1, cycle=1), MagicMock(id=2, cycle=2)]),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review._update_review_attempt", AsyncMock(),
    )

    # Dev server boots fine — the route just renders badly.
    monkeypatch.setattr(
        "agent.tools.dev_server.sniff_run_command",
        lambda ws, override=None: "npm run dev",
    )

    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(port=12345, log_path="/tmp/log", process=MagicMock(returncode=None))

    monkeypatch.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    monkeypatch.setattr("agent.tools.dev_server.wait_for_port", AsyncMock())

    # Reviewer agent: code OK, UI NOT-OK ("/broken returned 500").
    review_output = (
        '{"code_review": {"verdict": "OK", "reasoning": "code reads fine"}, '
        '"ui_check": {"verdict": "NOT-OK", "reasoning": "/broken returned 500"}}'
    )
    fake_agent = MagicMock(
        run=AsyncMock(return_value=MagicMock(
            output=review_output,
            tool_calls=[
                {"name": "browse_url", "args": {"url": "http://localhost:12345/broken"}},
            ],
        )),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review.create_agent",
        MagicMock(return_value=fake_agent),
    )

    publish_calls: list = []

    async def fake_publish(event):
        publish_calls.append(event)

    monkeypatch.setattr("agent.lifecycle.review.publish", fake_publish)

    transitions: list[tuple] = []

    async def fake_transition(task_id, status, message=""):
        transitions.append((task_id, status, message))

    monkeypatch.setattr(
        "agent.lifecycle.review.transition_task", fake_transition,
    )

    # Cycle 1 → coding loop-back.
    await review.handle_independent_review(9003, "http://pr", "b")
    # Cycle 2 → blocked.
    await review.handle_independent_review(9003, "http://pr", "b")

    # No task_review_complete(approved=True) was ever published.
    approved_events = [
        e for e in publish_calls
        if getattr(e, "payload", {}).get("approved") is True
    ]
    assert approved_events == []

    # Final transition went to BLOCKED.
    assert transitions[-1][1] == "blocked"
