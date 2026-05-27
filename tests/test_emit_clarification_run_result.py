"""Regression: ``_emit_clarification`` must persist messages from the
``AgentResult`` returned by ``agent.run()`` — not from ``agent.messages``,
which doesn't exist on ``AgentLoop``.

Task 29 (2026-05-27) wedged at ARCHITECT_BACKLOG_EMIT because
``_emit_clarification`` accessed ``agent.messages`` and raised
``AttributeError``. The exception was silently swallowed by the
fire-and-forget asyncio task that the orchestrator's
``on_design_approved`` handler spawned (also fixed in the same PR via
background-task retention). The architect emitted a clarification (the
design's brainstorming surfaced a tradeoff to confirm), the persistence
crashed, no session blob was written, and the task hung.

Pin: every call site passes the live ``run_result``; the helper reads
``messages`` / ``api_messages`` off it via ``getattr`` so future
shape changes degrade gracefully.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _FakeAgentResult:
    """Minimal stand-in for ``agent.loop.AgentResult``."""

    output: str = ""
    messages: list = field(default_factory=lambda: [{"role": "user", "content": "design pass"}])
    api_messages: list = field(default_factory=lambda: [{"role": "user", "content": "design pass"}])


@pytest.mark.asyncio
async def test_emit_clarification_persists_run_result_messages(tmp_path: Path):
    """The session blob written by _emit_clarification reflects the
    run_result's messages — not an AttributeError."""
    from agent.lifecycle.trio.architect import _emit_clarification
    from shared.models import ArchitectPhase

    # AgentLoop stand-in — deliberately DOES NOT expose `.messages` /
    # `.api_messages`, mirroring the real shape.
    agent = SimpleNamespace()
    workspace = SimpleNamespace(root=str(tmp_path))
    run_result = _FakeAgentResult(
        messages=[{"role": "assistant", "content": "I need to clarify..."}],
        api_messages=[{"role": "assistant", "content": "raw"}],
    )

    saved: dict = {}

    async def fake_save(messages, api_messages):
        saved["messages"] = messages
        saved["api_messages"] = api_messages

    # Stub out the DB / event side-effects — we only care about the
    # session-save path here.
    with (
        patch("agent.session.Session") as mock_session_cls,
        patch("orchestrator.state_machine.transition", new=AsyncMock()),
        patch("shared.events.publish", new=AsyncMock()),
        patch(
            "agent.lifecycle.trio.architect.async_session",
            new=MagicMock(),
        ) as mock_async_session,
    ):
        instance = MagicMock()
        instance.save = fake_save
        mock_session_cls.return_value = instance
        # Make the DB context manager return an async session that
        # silently no-ops the clarification-count query + commits.
        mock_session_cm = MagicMock()

        class _NoopExecResult:
            def scalar_one(self):
                return 0

        async def _exec(stmt):
            return _NoopExecResult()

        async def _commit():
            return None

        async def _refresh(row):
            return None

        async def _enter():
            return mock_session_cm

        async def _exit(*args):
            return False

        cm = MagicMock()
        cm.__aenter__ = _enter
        cm.__aexit__ = _exit
        mock_session_cm.execute = _exec
        mock_session_cm.commit = _commit
        mock_session_cm.add = lambda r: None
        mock_session_cm.refresh = _refresh
        mock_async_session.return_value = cm

        # Best-effort run — the helper hits many side-effects we've
        # stubbed; what we actually need to assert is that ``fake_save``
        # got the run_result's messages, NOT an AttributeError on
        # ``agent.messages``.
        try:
            await _emit_clarification(
                parent_task_id=999,
                agent=agent,
                workspace=workspace,
                output="hello",
                tool_calls=[],
                question="Which approach should we take?",
                phase=ArchitectPhase.INITIAL,
                run_result=run_result,
            )
        except AttributeError as exc:
            pytest.fail(f"AttributeError leaked from _emit_clarification: {exc}")
        except Exception:
            # Other failures (DB stubs, event bus, etc.) are not what
            # this test is pinning — the session-save call has already
            # been exercised by this point.
            pass

    assert "messages" in saved, "Session.save was never called"
    assert saved["messages"] == run_result.messages
    assert saved["api_messages"] == run_result.api_messages
