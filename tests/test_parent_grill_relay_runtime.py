"""Phase 8.5 runtime — real ``_ask_parent_to_answer_grill`` resume path.

Phase 8 (commit ``caae043``) shipped ``_ask_parent_to_answer_grill`` as
a stub. Phase 8.5 (this file) lands the production resume of the
parent architect's persisted session and pins it via tests that mock
at the LLM seam (``create_architect_agent``).

What we pin:

  1. The parent's session blob is loaded if present (``trio-<id>.json``
     under workspace_root).
  2. The architect agent is built with ``resume=True`` and the prompt
     spelling out the slice question.
  3. When the parent's response writes
     ``slices/<name>/grill_answer.json`` the function returns success
     without raising.
  4. When the parent fails to write the file, the function retries
     ONCE with an amended prompt instructing the architect to call the
     ``submit-grill-answer`` skill.
  5. After two misses, :class:`sub_architect.MissingGrillAnswerError`
     is raised so the dispatcher can fail the slice.
  6. The parent's session is persisted again after each turn —
     symmetric with Phase 6's ``_persist_architect_session``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from agent.lifecycle.trio import sub_architect

if TYPE_CHECKING:
    from pathlib import Path


def _parent_stub(parent_id: int = 7):
    class _Parent:
        id = parent_id
        title = "build a TODO app"
        description = "Build TODO"
        repo = None
        organization_id = 1
        created_by_user_id = 1

    return _Parent()


class _FakeParentAgent:
    """Fake AgentLoop for the parent grill relay.

    On ``run`` it invokes a configured writer (which simulates the
    submit-grill-answer skill call). Records every prompt + resume flag
    so tests can assert the relay called with ``resume=True``.
    """

    def __init__(self, writer):
        self._writer = writer
        self.messages: list[Any] = []
        self.api_messages: list[Any] = []
        self.run_calls: list[dict[str, Any]] = []

    async def run(self, prompt: str, *, resume: bool = False, **_kw):
        from agent.llm.types import Message

        self.run_calls.append({"prompt": prompt, "resume": resume})
        if self._writer is not None:
            self._writer(prompt=prompt, resume=resume, turn=len(self.run_calls))

        class R:
            output = "ok"

        self.messages.append(Message(role="user", content=prompt))
        self.messages.append(Message(role="assistant", content="ok"))
        self.api_messages = list(self.messages)
        return R()


# ---------------------------------------------------------------------------
# 1. Happy path — parent writes grill_answer.json on first turn.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_succeeds_when_parent_writes_answer(tmp_path: Path) -> None:
    workspace = tmp_path

    def writer(*, prompt, resume, turn):
        target = workspace / ".auto-agent" / "slices" / "auth" / "grill_answer.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"schema_version": "1", "answer": "Use Clerk."}))

    factory_calls: list[dict[str, Any]] = []

    def fake_factory(**kwargs):
        factory_calls.append(kwargs)
        return _FakeParentAgent(writer)

    with patch.object(sub_architect, "create_architect_agent", fake_factory):
        # Should not raise.
        await sub_architect._ask_parent_to_answer_grill(
            parent_task=_parent_stub(),
            slice_name="auth",
            question="Which auth provider?",
            workspace_root=str(workspace),
        )

    # The answer file was written.
    assert (workspace / ".auto-agent" / "slices" / "auth" / "grill_answer.json").is_file()
    # The architect agent was built once (no retry needed).
    assert len(factory_calls) == 1


# ---------------------------------------------------------------------------
# 2. Resume flag — agent.run is called with resume=True.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_resumes_parent_session(tmp_path: Path) -> None:
    """The architect must be invoked with ``resume=True`` so its prior
    design + decision history are visible. Confirms the parent-session
    reload contract from ADR-015 §10."""

    workspace = tmp_path

    captured: list[_FakeParentAgent] = []

    def writer(*, prompt, resume, turn):
        target = workspace / ".auto-agent" / "slices" / "auth" / "grill_answer.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"schema_version": "1", "answer": "x"}))

    def fake_factory(**kwargs):
        agent = _FakeParentAgent(writer)
        captured.append(agent)
        return agent

    with patch.object(sub_architect, "create_architect_agent", fake_factory):
        await sub_architect._ask_parent_to_answer_grill(
            parent_task=_parent_stub(),
            slice_name="auth",
            question="?",
            workspace_root=str(workspace),
        )

    assert captured
    # Every call to .run must pass resume=True.
    for agent in captured:
        for call in agent.run_calls:
            assert call["resume"] is True, "parent grill relay must resume the session"


# ---------------------------------------------------------------------------
# 3. Question is embedded in the prompt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_carries_slice_question(tmp_path: Path) -> None:
    workspace = tmp_path

    def writer(*, prompt, resume, turn):
        target = workspace / ".auto-agent" / "slices" / "auth" / "grill_answer.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"schema_version": "1", "answer": "x"}))

    fake = _FakeParentAgent(writer)
    with patch.object(sub_architect, "create_architect_agent", lambda **_kw: fake):
        await sub_architect._ask_parent_to_answer_grill(
            parent_task=_parent_stub(),
            slice_name="auth",
            question="Which OAuth library should I pick?",
            workspace_root=str(workspace),
        )

    assert any("Which OAuth library should I pick?" in call["prompt"] for call in fake.run_calls)
    assert any("submit-grill-answer" in call["prompt"] for call in fake.run_calls)


# ---------------------------------------------------------------------------
# 4. Missing answer file → retry once.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_retries_once_when_answer_missing(tmp_path: Path) -> None:
    workspace = tmp_path

    # Cross-agent counter — the relay creates a fresh AgentLoop per
    # attempt, so a per-agent ``turn`` resets each time. We need a
    # global view of "which overall attempt is this".
    attempt_counter = {"n": 0}

    def writer(*, prompt, resume, turn):
        attempt_counter["n"] += 1
        if attempt_counter["n"] >= 2:
            # On the second overall attempt, write the answer.
            target = workspace / ".auto-agent" / "slices" / "auth" / "grill_answer.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"schema_version": "1", "answer": "x"}))

    captured: list[_FakeParentAgent] = []

    def fake_factory(**_kw):
        agent = _FakeParentAgent(writer)
        captured.append(agent)
        return agent

    with patch.object(sub_architect, "create_architect_agent", fake_factory):
        await sub_architect._ask_parent_to_answer_grill(
            parent_task=_parent_stub(),
            slice_name="auth",
            question="?",
            workspace_root=str(workspace),
        )

    # We expect two run invocations (1 initial + 1 retry) — across one
    # or two architect-agent factory invocations is implementation
    # detail; what matters is that the model was asked twice and the
    # second prompt was amended.
    total_calls = sum(len(a.run_calls) for a in captured)
    assert total_calls == 2

    second_call_prompt = None
    seen = 0
    for agent in captured:
        for call in agent.run_calls:
            seen += 1
            if seen == 2:
                second_call_prompt = call["prompt"]
                break
        if second_call_prompt is not None:
            break

    assert second_call_prompt is not None
    assert "submit-grill-answer" in second_call_prompt
    # The retry prompt amends with a stronger directive than the first.
    assert "MUST" in second_call_prompt or "must" in second_call_prompt.lower()


# ---------------------------------------------------------------------------
# 5. Missing answer after 2 attempts → MissingGrillAnswerError.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_answer_after_retry_raises(tmp_path: Path) -> None:
    workspace = tmp_path

    def writer(*, prompt, resume, turn):
        # Never write the answer — both attempts miss.
        return

    captured: list[_FakeParentAgent] = []

    def fake_factory(**_kw):
        agent = _FakeParentAgent(writer)
        captured.append(agent)
        return agent

    with (
        patch.object(sub_architect, "create_architect_agent", fake_factory),
        pytest.raises(sub_architect.MissingGrillAnswerError),
    ):
        await sub_architect._ask_parent_to_answer_grill(
            parent_task=_parent_stub(),
            slice_name="auth",
            question="?",
            workspace_root=str(workspace),
        )

    total_calls = sum(len(a.run_calls) for a in captured)
    assert total_calls == 2, "exactly 2 attempts before raising"


# ---------------------------------------------------------------------------
# 6. Parent's session blob is persisted after each turn so the
#    round-trip is durable (mirrors Phase 6 _persist_architect_session).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parent_session_persisted_after_turn(tmp_path: Path) -> None:
    workspace = tmp_path

    def writer(*, prompt, resume, turn):
        target = workspace / ".auto-agent" / "slices" / "auth" / "grill_answer.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"schema_version": "1", "answer": "x"}))

    with patch.object(
        sub_architect, "create_architect_agent", lambda **_kw: _FakeParentAgent(writer)
    ):
        await sub_architect._ask_parent_to_answer_grill(
            parent_task=_parent_stub(parent_id=7),
            slice_name="auth",
            question="?",
            workspace_root=str(workspace),
        )

    # session.save writes the json blob under the workspace dir.
    session_blob = workspace / "trio-7.json"
    assert session_blob.is_file(), "parent session must be persisted after the relay"


# ---------------------------------------------------------------------------
# 7. Stale answer file is cleared before the relay so a leftover from
#    a prior round cannot satisfy this turn.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_answer_file_is_removed_before_relay(tmp_path: Path) -> None:
    workspace = tmp_path

    target = workspace / ".auto-agent" / "slices" / "auth" / "grill_answer.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"schema_version": "1", "answer": "STALE"}))

    def writer(*, prompt, resume, turn):
        # Never write fresh — should raise because the stale file was wiped.
        return

    with (
        patch.object(
            sub_architect, "create_architect_agent", lambda **_kw: _FakeParentAgent(writer)
        ),
        pytest.raises(sub_architect.MissingGrillAnswerError),
    ):
        await sub_architect._ask_parent_to_answer_grill(
            parent_task=_parent_stub(),
            slice_name="auth",
            question="?",
            workspace_root=str(workspace),
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
