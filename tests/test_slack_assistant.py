"""Tests for the LLM tool-loop primitive `converse`."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.llm.types import LLMResponse, Message, ToolCall
from agent.slack_assistant import converse

pytestmark = pytest.mark.asyncio


def _resp(
    content: str = "", tool_calls: list[ToolCall] | None = None, stop_reason: str = "end_turn"
) -> LLMResponse:
    return LLMResponse(
        message=Message(role="assistant", content=content, tool_calls=tool_calls or []),
        stop_reason=stop_reason,
        usage=None,
    )


async def test_converse_returns_appended_messages_and_reply():
    history: list[Message] = []
    fake_provider = AsyncMock()
    fake_provider.complete.return_value = _resp(content="hello!")
    with (
        patch("agent.slack_assistant.get_provider", return_value=fake_provider),
        patch("agent.slack_assistant.resolve_home_dir", return_value=None),
    ):
        reply, appended = await converse(
            user_id=1,
            text="hi",
            history=history,
            home_dir=None,
            on_create_task=None,
        )
    assert reply == "hello!"
    # appended = the user msg + the assistant reply (2 entries)
    assert len(appended) == 2
    assert appended[0].role == "user"
    assert appended[1].role == "assistant"


async def test_converse_forces_bedrock_provider_to_dodge_claude_cli_passthrough():
    """With LLM_PROVIDER=claude_cli on the VM, the pass-through ignores
    ``tools=...`` so the assistant's approve/reject/etc. tool calls never
    happen — Claude Code generates free-form prose instead. The fix is
    the same escape hatch the structured-extractor uses: force a
    tool-capable provider regardless of the project default.
    """
    history: list[Message] = []
    fake_provider = AsyncMock()
    fake_provider.complete.return_value = _resp(content="hello!")
    with (
        patch("agent.slack_assistant.get_provider", return_value=fake_provider) as gp,
        patch("agent.slack_assistant.resolve_home_dir", return_value=None),
    ):
        await converse(
            user_id=1,
            text="hi",
            history=history,
            home_dir=None,
            on_create_task=None,
        )
    gp.assert_called_once()
    kwargs = gp.call_args.kwargs
    assert kwargs.get("provider_override") == "bedrock", (
        f"converse must escape claude_cli; got kwargs={kwargs}"
    )


async def test_approve_plan_tool_targets_gate_approval_endpoint():
    """``_approve_plan`` must hit ``/tasks/{id}/approve-plan`` (ADR-015 §6 —
    handles both AWAITING_PLAN_APPROVAL and AWAITING_DESIGN_APPROVAL), not
    the legacy ``/tasks/{id}/approve`` which only accepts AWAITING_APPROVAL
    and 400s on design gates. Production repro: task 5 sitting in
    AWAITING_DESIGN_APPROVAL on 2026-05-15 — user said "approved", the
    tool call would have 400'd."""
    from agent.slack_assistant import _approve_plan

    fake_client = AsyncMock()
    fake_resp = AsyncMock()
    fake_resp.status_code = 200
    fake_resp.text = "{}"
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.post = AsyncMock(return_value=fake_resp)

    with (
        patch("agent.slack_assistant.httpx.AsyncClient", return_value=fake_client),
        patch(
            "agent.slack_assistant._internal_auth_headers",
            AsyncMock(return_value={"Authorization": "Bearer test-token"}),
        ),
    ):
        await _approve_plan(task_id=5, user_id=1, feedback="lgtm")

    fake_client.post.assert_called_once()
    url = fake_client.post.call_args.args[0]
    assert url.endswith("/tasks/5/approve-plan"), f"expected /approve-plan, got {url}"


async def test_internal_auth_headers_returns_bearer_token():
    """``_internal_auth_headers`` must mint a Bearer token so the orchestrator's
    org-scoped dependency lets the request through. Production repro
    2026-05-15: unauthenticated GET /api/tasks → 401 → empty list → AI told
    user there were no tasks."""
    from unittest.mock import MagicMock

    from agent.slack_assistant import _internal_auth_headers

    fake_user = MagicMock()
    fake_user.id = 1
    fake_user.username = "alan"

    # Two execute() calls: the user lookup (scalar_one_or_none → user),
    # then the org_id lookup (scalar_one_or_none → 1).
    def _execute_side_effect(_stmt):
        result = MagicMock()
        if _execute_side_effect.calls == 0:
            result.scalar_one_or_none = MagicMock(return_value=fake_user)
        else:
            result.scalar_one_or_none = MagicMock(return_value=1)
        _execute_side_effect.calls += 1
        return result

    _execute_side_effect.calls = 0

    fake_session = AsyncMock()
    fake_session.execute = AsyncMock(side_effect=_execute_side_effect)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_async_session():
        yield fake_session

    with patch("shared.database.async_session", fake_async_session):
        headers = await _internal_auth_headers(user_id=1)

    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Bearer ")


async def test_converse_injects_current_focus_into_system_prompt():
    """When the messenger router has a task focus, ``converse`` should
    surface it in the system prompt so the AI calls approve/cancel on
    that specific task instead of asking which task or listing all
    tasks. Production repro 2026-05-15: focus was set to task 5
    (AWAITING_DESIGN_APPROVAL), user said "approve", the AI ran
    list_my_tasks(status="awaiting_approval") → empty list (wrong status
    name) → "no tasks awaiting approval" because converse never told it
    which task we were on.

    Note: this fixture mirrors the *real* ``_get_task`` success-path
    shape — which ALWAYS includes ``"error": ""`` even on success. An
    earlier version of ``_build_system_prompt`` used ``"error" in task``
    to detect failure, which is true for every result, so the focus
    block was silently never injected.
    """
    history: list[Message] = []
    fake_provider = AsyncMock()
    fake_provider.complete.return_value = _resp(content="ok")
    with (
        patch("agent.slack_assistant.get_provider", return_value=fake_provider),
        patch("agent.slack_assistant.resolve_home_dir", return_value=None),
        patch(
            "agent.slack_assistant._get_task",
            AsyncMock(
                return_value={
                    "id": 5,
                    "title": "Parallel universe screen",
                    "description": "...",
                    "status": "AWAITING_DESIGN_APPROVAL",
                    "repo": "iot-apartment-simulator",
                    "plan": "",
                    "pr_url": None,
                    "error": "",  # real shape: always present
                    "created_by_user_id": 1,
                }
            ),
        ),
    ):
        await converse(
            user_id=1,
            text="approve",
            history=history,
            home_dir=None,
            on_create_task=None,
            current_focus={"kind": "task", "id": 5},
        )

    fake_provider.complete.assert_awaited()
    system = fake_provider.complete.await_args.kwargs["system"]
    assert "task #5" in system or "task_id=5" in system, "focus context missing from system prompt"
    assert "AWAITING_DESIGN_APPROVAL" in system
    assert "Parallel universe screen" in system


async def test_converse_no_focus_keeps_baseline_system_prompt():
    """No focus → no per-task addendum; baseline prompt only."""
    fake_provider = AsyncMock()
    fake_provider.complete.return_value = _resp(content="ok")
    with (
        patch("agent.slack_assistant.get_provider", return_value=fake_provider),
        patch("agent.slack_assistant.resolve_home_dir", return_value=None),
    ):
        await converse(
            user_id=1,
            text="hi",
            history=[],
            home_dir=None,
            on_create_task=None,
        )
    system = fake_provider.complete.await_args.kwargs["system"]
    # Baseline prompt has no per-task addendum marker.
    assert "Current task context" not in system


async def test_converse_invokes_on_create_task_when_create_task_tool_fires():
    history: list[Message] = []
    fake_provider = AsyncMock()
    fake_provider.complete.side_effect = [
        _resp(
            tool_calls=[
                ToolCall(
                    id="t1",
                    name="create_task",
                    arguments={
                        "repo_name": "cardamon",
                        "description": "test task",
                    },
                )
            ],
            stop_reason="tool_use",
        ),
        _resp(content="created!"),
    ]
    on_create_task = AsyncMock()
    with (
        patch("agent.slack_assistant.get_provider", return_value=fake_provider),
        patch("agent.slack_assistant.resolve_home_dir", return_value=None),
        patch(
            "agent.slack_assistant._create_task",
            AsyncMock(return_value={"task_id": 77, "status": "queued", "title": "x"}),
        ),
    ):
        reply, _ = await converse(
            user_id=1,
            text="create a test task on cardamon",
            history=history,
            home_dir=None,
            on_create_task=on_create_task,
        )
    assert reply == "created!"
    on_create_task.assert_awaited_once_with(77)
