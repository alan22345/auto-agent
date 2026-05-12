"""Tests for the review-phase extensions:

- T20: ReviewAttempt rows are persisted per invocation.
- T21: UI-check sub-step boots the dev server when affected_routes resolves, and
  the reviewer's structured combined verdict is parsed.
- T22: Rejection on cycle 1 loops back to CODING; on cycle 2 lands in BLOCKED.

The tests patch the ReviewAttempt DB helpers so we don't need a real Postgres —
matches the style of ``tests/test_verify_phase.py``.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from agent.lifecycle import review

# --- helpers ---


def _patch_orchestrator_io(monkeypatch, task, repo=None, freeform_cfg=None):
    """Patch everything outside review.py: orchestrator API, workspace clone, shell."""
    repo = repo or MagicMock(default_branch="main", url="https://github.com/x/y")
    monkeypatch.setattr(
        "agent.lifecycle.review.get_task", AsyncMock(return_value=task),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review.get_repo", AsyncMock(return_value=repo),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review.get_freeform_config",
        AsyncMock(return_value=freeform_cfg),
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
    monkeypatch.setattr(
        "agent.lifecycle.review.publish", AsyncMock(),
    )


def _patch_db(monkeypatch, *, cycle=1):
    """Patch ReviewAttempt DB helpers; return an `updates` list capturing kwargs."""
    attempt = MagicMock(id=1, cycle=cycle)
    monkeypatch.setattr(
        "agent.lifecycle.review._next_review_cycle",
        AsyncMock(return_value=cycle),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review._create_review_attempt",
        AsyncMock(return_value=attempt),
    )
    updates: list[dict] = []

    async def fake_update(_attempt_id, *, finished=False, **fields):
        updates.append({"finished": finished, **fields})

    monkeypatch.setattr(
        "agent.lifecycle.review._update_review_attempt", fake_update,
    )
    return updates


def _patch_create_agent(monkeypatch, output: str, tool_calls=None):
    """Replace create_agent with a stub that returns the given output."""
    tool_calls = tool_calls or []
    fake_agent = MagicMock(
        run=AsyncMock(
            return_value=MagicMock(output=output, tool_calls=tool_calls),
        ),
    )
    monkeypatch.setattr(
        "agent.lifecycle.review.create_agent",
        MagicMock(return_value=fake_agent),
    )
    return fake_agent


# --- T20: persistence ---


async def test_review_passes_records_attempt(monkeypatch):
    """Reviewer approves → ReviewAttempt update with status=pass."""
    task = MagicMock(
        id=1001, title="t", description="d", repo_name="r",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[],
    )
    _patch_orchestrator_io(monkeypatch, task)
    updates = _patch_db(monkeypatch, cycle=1)
    _patch_create_agent(
        monkeypatch,
        output='{"code_review": {"verdict": "OK", "reasoning": "looks good"}, '
               '"ui_check": {"verdict": "SKIPPED", "reasoning": "no UI"}}',
    )

    await review.handle_independent_review(1001, "http://pr", "b")

    # Final update should mark it passed.
    finals = [u for u in updates if u.get("finished")]
    assert len(finals) >= 1
    assert finals[-1]["status"] == "pass"
    assert "looks good" in finals[-1]["code_review_verdict"]
    assert finals[-1]["ui_check"] == "skipped"


async def test_review_keyword_fallback_records_attempt(monkeypatch):
    """Free-form 'lgtm' output still parses as approved via the legacy fallback."""
    task = MagicMock(
        id=1002, title="t", description="d", repo_name="r",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[],
    )
    _patch_orchestrator_io(monkeypatch, task)
    updates = _patch_db(monkeypatch, cycle=1)
    _patch_create_agent(monkeypatch, output="lgtm — approved")

    await review.handle_independent_review(1002, "http://pr", "b")

    finals = [u for u in updates if u.get("finished")]
    assert finals[-1]["status"] == "pass"


# --- T21: UI check ---


async def test_ui_check_skipped_when_no_routes(monkeypatch):
    """No declared routes → no server start, ui_check='skipped' on the attempt."""
    task = MagicMock(
        id=1003, title="t", description="d", repo_name="r",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[],
    )
    _patch_orchestrator_io(monkeypatch, task)
    updates = _patch_db(monkeypatch, cycle=1)
    sniff = MagicMock()
    monkeypatch.setattr("agent.tools.dev_server.sniff_run_command", sniff)
    _patch_create_agent(
        monkeypatch,
        output='{"code_review": {"verdict": "OK", "reasoning": "good"}, '
               '"ui_check": {"verdict": "SKIPPED", "reasoning": ""}}',
    )

    await review.handle_independent_review(1003, "http://pr", "b")

    # sniff_run_command should not be called when there are no routes.
    sniff.assert_not_called()
    finals = [u for u in updates if u.get("finished")]
    assert finals[-1]["ui_check"] == "skipped"


async def test_ui_check_runs_when_routes_present(monkeypatch):
    """Routes + sniffable runner → server boots, verdict UI is passed through."""
    task = MagicMock(
        id=1004, title="t", description="d", repo_name="r",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[{"method": "GET", "path": "/", "label": "home"}],
    )
    _patch_orchestrator_io(monkeypatch, task)
    updates = _patch_db(monkeypatch, cycle=1)
    monkeypatch.setattr(
        "agent.tools.dev_server.sniff_run_command",
        lambda ws, override=None: "npm run dev",
    )

    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(
            port=12345, log_path="/tmp/log",
            process=MagicMock(returncode=None),
        )

    monkeypatch.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    monkeypatch.setattr(
        "agent.tools.dev_server.wait_for_port", AsyncMock(),
    )
    _patch_create_agent(
        monkeypatch,
        output='{"code_review": {"verdict": "OK", "reasoning": "good"}, '
               '"ui_check": {"verdict": "OK", "reasoning": "renders cleanly"}}',
        tool_calls=[{"name": "browse_url", "args": {"url": "http://localhost:12345/"}}],
    )

    await review.handle_independent_review(1004, "http://pr", "b")

    finals = [u for u in updates if u.get("finished")]
    assert finals[-1]["status"] == "pass"
    assert finals[-1]["ui_check"] == "pass"
    assert "renders cleanly" in finals[-1]["ui_judgment"]
    assert finals[-1]["tool_calls"]  # non-empty


async def test_ui_check_skipped_publishes_event_when_no_runner(monkeypatch):
    """Routes declared but no run command → publish review_skipped_no_runner."""
    task = MagicMock(
        id=1005, title="t", description="d", repo_name="r",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[{"method": "GET", "path": "/", "label": "home"}],
    )
    _patch_orchestrator_io(monkeypatch, task)
    _patch_db(monkeypatch, cycle=1)
    monkeypatch.setattr(
        "agent.tools.dev_server.sniff_run_command",
        lambda ws, override=None: None,
    )
    _patch_create_agent(
        monkeypatch,
        output='{"code_review": {"verdict": "OK", "reasoning": "good"}, '
               '"ui_check": {"verdict": "SKIPPED", "reasoning": "no runner"}}',
    )

    publish_calls = []

    async def fake_publish(event):
        publish_calls.append(event.type)

    monkeypatch.setattr("agent.lifecycle.review.publish", fake_publish)

    await review.handle_independent_review(1005, "http://pr", "b")

    assert "task.review_skipped_no_runner" in publish_calls


# --- T22: cycle budget ---


async def test_rejection_cycle_1_transitions_to_coding(monkeypatch):
    task = MagicMock(
        id=1006, title="t", description="d", repo_name="r",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[],
    )
    _patch_orchestrator_io(monkeypatch, task)
    _patch_db(monkeypatch, cycle=1)
    _patch_create_agent(
        monkeypatch,
        output='{"code_review": {"verdict": "NOT-OK", "reasoning": "bad"}, '
               '"ui_check": {"verdict": "SKIPPED", "reasoning": ""}}',
    )

    transitions = []

    async def fake_transition(task_id, status, message=""):
        transitions.append((task_id, status, message))

    monkeypatch.setattr(
        "agent.lifecycle.review.transition_task", fake_transition,
    )

    await review.handle_independent_review(1006, "http://pr", "b")

    assert transitions[-1][:2] == (1006, "coding")


async def test_rejection_cycle_2_transitions_to_blocked(monkeypatch):
    task = MagicMock(
        id=1007, title="t", description="d", repo_name="r",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[],
    )
    _patch_orchestrator_io(monkeypatch, task)
    _patch_db(monkeypatch, cycle=2)
    _patch_create_agent(
        monkeypatch,
        output='{"code_review": {"verdict": "NOT-OK", "reasoning": "still bad"}, '
               '"ui_check": {"verdict": "SKIPPED", "reasoning": ""}}',
    )

    transitions = []

    async def fake_transition(task_id, status, message=""):
        transitions.append((task_id, status, message))

    monkeypatch.setattr(
        "agent.lifecycle.review.transition_task", fake_transition,
    )

    await review.handle_independent_review(1007, "http://pr", "b")

    assert transitions[-1][:2] == (1007, "blocked")


async def test_ui_rejection_uses_ui_judgment_not_ok_reason(monkeypatch):
    task = MagicMock(
        id=1008, title="t", description="d", repo_name="r",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[{"method": "GET", "path": "/", "label": "home"}],
    )
    _patch_orchestrator_io(monkeypatch, task)
    updates = _patch_db(monkeypatch, cycle=1)
    monkeypatch.setattr(
        "agent.tools.dev_server.sniff_run_command",
        lambda ws, override=None: "npm run dev",
    )

    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(
            port=12345, log_path="/tmp/log",
            process=MagicMock(returncode=None),
        )

    monkeypatch.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    monkeypatch.setattr("agent.tools.dev_server.wait_for_port", AsyncMock())
    _patch_create_agent(
        monkeypatch,
        output='{"code_review": {"verdict": "OK", "reasoning": "fine"}, '
               '"ui_check": {"verdict": "NOT-OK", "reasoning": "/ returns 500"}}',
    )
    monkeypatch.setattr("agent.lifecycle.review.transition_task", AsyncMock())

    await review.handle_independent_review(1008, "http://pr", "b")

    finals = [u for u in updates if u.get("finished")]
    assert finals[-1]["status"] == "fail"
    assert finals[-1]["failure_reason"] == "ui_judgment_not_ok"


async def test_boot_failure_records_attempt_and_loops_back(monkeypatch):
    """Dev-server boot timeout during UI setup → attempt fails, transition fires."""
    task = MagicMock(
        id=1009, title="t", description="d", repo_name="r",
        freeform_mode=True, created_by_user_id=None, organization_id=1,
        affected_routes=[{"method": "GET", "path": "/", "label": "home"}],
    )
    _patch_orchestrator_io(monkeypatch, task)
    updates = _patch_db(monkeypatch, cycle=1)
    monkeypatch.setattr(
        "agent.tools.dev_server.sniff_run_command",
        lambda ws, override=None: "npm run dev",
    )

    from agent.tools.dev_server import BootTimeout

    @asynccontextmanager
    async def fake_start(ws, override=None):
        yield MagicMock(
            port=12345, log_path="/tmp/log",
            process=MagicMock(returncode=None),
        )

    monkeypatch.setattr("agent.tools.dev_server.start_dev_server", fake_start)
    monkeypatch.setattr(
        "agent.tools.dev_server.wait_for_port",
        AsyncMock(side_effect=BootTimeout("boom")),
    )
    transitions = []

    async def fake_transition(task_id, status, message=""):
        transitions.append((task_id, status, message))

    monkeypatch.setattr(
        "agent.lifecycle.review.transition_task", fake_transition,
    )

    await review.handle_independent_review(1009, "http://pr", "b")

    finals = [u for u in updates if u.get("finished")]
    assert finals[-1]["status"] == "fail"
    assert finals[-1]["failure_reason"] == "boot_timeout"
    assert transitions[-1][:2] == (1009, "coding")
