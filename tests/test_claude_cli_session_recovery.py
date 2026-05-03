"""Tests for the Claude CLI provider's session-already-in-use recovery.

When the CLI rejects a session ID with "Session ID ... is already in use"
(stale lock from a crashed prior invocation, or two callers passing the
same deterministic hash), the provider rotates ``self._session_id`` to a
fresh UUID and retries once. Without this, the error string leaked back
as if it were an LLM response and downstream code (e.g. the independent
reviewer) treated it as legitimate feedback.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from agent.llm.claude_cli import ClaudeCLIProvider, _SESSION_ALREADY_IN_USE


@pytest.mark.asyncio
async def test_run_cli_rotates_session_on_already_in_use_and_retries(monkeypatch):
    """First call hits 'already in use', provider rotates, retry succeeds."""
    provider = ClaudeCLIProvider()
    provider.set_session("ab624c01-83a1-5f18-aeb3-5d50bbf53b0f", resume=False)
    original = provider._session_id

    calls: list[str | None] = []

    async def fake_invoke(prompt: str):
        calls.append(provider._session_id)
        if len(calls) == 1:
            return ("", f"Error: Session ID {original} is {_SESSION_ALREADY_IN_USE}.", 1)
        return ("Looks good to me — LGTM", "", 0)

    monkeypatch.setattr(provider, "_invoke_cli_once", fake_invoke)

    result = await provider._run_cli("review this PR")

    assert "LGTM" in result
    assert len(calls) == 2, "should retry exactly once"
    assert calls[0] == original
    assert calls[1] != original, "session ID must rotate on retry"
    # The new session ID is a valid UUID
    uuid.UUID(provider._session_id)


@pytest.mark.asyncio
async def test_run_cli_does_not_rotate_in_resume_mode(monkeypatch):
    """--resume EXPECTS the session to exist; rotating would break the lookup.
    A failure in resume mode propagates rather than retrying with a new ID."""
    provider = ClaudeCLIProvider()
    provider.set_session("ab624c01-83a1-5f18-aeb3-5d50bbf53b0f", resume=True)

    calls: list[str | None] = []

    async def fake_invoke(prompt: str):
        calls.append(provider._session_id)
        return ("", f"Error: Session ID is {_SESSION_ALREADY_IN_USE}.", 1)

    monkeypatch.setattr(provider, "_invoke_cli_once", fake_invoke)

    result = await provider._run_cli("continue task")

    assert "[ERROR] CLI exited 1" in result
    assert len(calls) == 1, "no retry in resume mode"


@pytest.mark.asyncio
async def test_run_cli_does_not_rotate_on_other_errors(monkeypatch):
    """Errors that aren't 'already in use' don't trigger the retry path."""
    provider = ClaudeCLIProvider()
    provider.set_session("some-id", resume=False)

    calls = 0

    async def fake_invoke(prompt: str):
        nonlocal calls
        calls += 1
        return ("", "Error: billing limit exceeded", 1)

    monkeypatch.setattr(provider, "_invoke_cli_once", fake_invoke)

    result = await provider._run_cli("hi")

    assert "[ERROR] CLI exited 1" in result
    assert "billing limit exceeded" in result
    assert calls == 1, "non-collision errors should not retry"


@pytest.mark.asyncio
async def test_run_cli_succeeds_first_time_no_retry(monkeypatch):
    """Happy path — single invocation, no rotation."""
    provider = ClaudeCLIProvider()
    provider.set_session("happy-id", resume=False)
    original = provider._session_id

    calls = 0

    async def fake_invoke(prompt: str):
        nonlocal calls
        calls += 1
        return ("real LLM response", "", 0)

    monkeypatch.setattr(provider, "_invoke_cli_once", fake_invoke)

    result = await provider._run_cli("hi")

    assert result == "real LLM response"
    assert calls == 1
    assert provider._session_id == original, "session ID must NOT rotate on success"


@pytest.mark.asyncio
async def test_run_cli_handles_timeout(monkeypatch):
    """Timeout (returncode is None) returns the timeout marker, no retry."""
    provider = ClaudeCLIProvider()
    provider.set_session("timeout-id", resume=False)

    calls = 0

    async def fake_invoke(prompt: str):
        nonlocal calls
        calls += 1
        return ("", "", None)

    monkeypatch.setattr(provider, "_invoke_cli_once", fake_invoke)

    result = await provider._run_cli("hi")

    assert "timed out" in result.lower()
    assert calls == 1
