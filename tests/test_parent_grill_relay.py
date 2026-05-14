"""Parent-answers-grill relay for sub-architects — ADR-015 §10 / Phase 8.

Sub-architect grill questions go UP to the parent architect, never to
the user or a freeform standin. The relay is file-based via the skills
bridge:

  sub-architect writes  → slices/<name>/grill_question.json
  orchestrator resumes parent architect's session with the question
  parent writes          → slices/<name>/grill_answer.json
  orchestrator re-invokes sub-architect with the answer prepended

Three behaviours pinned here:

1. The relay reads grill_question.json, resumes the parent's session,
   and asks the parent (via a new orchestrator entry point) — the
   answer ends up in slices/<name>/grill_answer.json.
2. User / freeform-standin is NEVER invoked during a parent-relay.
   In particular the existing ``architect._emit_clarification`` path
   (which transitions to AWAITING_CLARIFICATION) must not fire.
3. Multiple grill rounds in one sub-architect run each round-trip the
   parent independently.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest


def _parent_stub(parent_id: int = 99):
    class _Parent:
        id = parent_id
        title = "build a TODO app"
        description = "Build TODO"
        repo = None
        organization_id = 1
        created_by_user_id = 1

    return _Parent()


def _make_relay_test_runner(workspace_root: Path, *, question_rounds: int = 1):
    """Build a fake sub-architect runner that emits a grill question on
    its first ``question_rounds`` invocations, then completes.

    The runner keeps per-slice state in a dict captured in the closure
    so the dispatcher can call it multiple times for the same slice
    (once to ask, once after the answer is written).
    """

    state: dict[str, int] = {}

    async def runner(
        *,
        parent_task,
        slice_spec,
        workspace_root,
        slice_root,
        **_kw,
    ) -> dict[str, Any]:
        name = slice_spec["name"]
        attempt = state.get(name, 0) + 1
        state[name] = attempt
        slice_path = Path(slice_root)
        slice_path.mkdir(parents=True, exist_ok=True)

        if attempt <= question_rounds:
            (slice_path / "grill_question.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "question": (f"slice {name} clarification round {attempt}: which DB?"),
                    }
                )
            )
            return {"status": "paused_for_grill"}

        # After the relay completes, the sub-architect "resumes" and
        # finishes by writing its design + backlog.
        (slice_path / "design.md").write_text(f"# Slice {name}\nGot answers; built design.\n")
        (slice_path / "backlog.json").write_text(
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
        return {"status": "completed"}

    return runner, state


# ---------------------------------------------------------------------------
# 1. Relay round-trip: parent answers the sub-architect's grill question.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parent_answers_sub_architect_grill(tmp_path: Path) -> None:
    from agent.lifecycle.trio import sub_architect

    runner, _ = _make_relay_test_runner(tmp_path, question_rounds=1)

    captured_questions: list[str] = []

    async def fake_ask_parent(*, parent_task, slice_name, question, workspace_root):
        """Stand-in for the orchestrator hook that resumes the parent's
        session and asks them. Writes the answer to grill_answer.json."""

        captured_questions.append(question)
        ans_path = (
            Path(workspace_root) / ".auto-agent" / "slices" / slice_name / "grill_answer.json"
        )
        ans_path.parent.mkdir(parents=True, exist_ok=True)
        ans_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "answer": f"answer to '{question[:40]}': postgres",
                }
            )
        )

    slices = [{"name": "auth", "scope": "auth"}]

    with (
        patch.object(sub_architect, "_run_sub_architect_slice", runner),
        patch.object(sub_architect, "_ask_parent_to_answer_grill", fake_ask_parent),
    ):
        result = await sub_architect.dispatch_sub_architects(
            parent_task=_parent_stub(),
            workspace_root=str(tmp_path),
            slices=slices,
        )

    # The question landed in front of the parent-asker hook.
    assert captured_questions
    assert "which DB" in captured_questions[0]
    # The relay closed the loop — the slice ultimately completed.
    assert result.ok is True
    auth = next(s for s in result.slice_results if s.name == "auth")
    assert auth.status == "completed"

    # The answer file was written under the slice namespace.
    answer = json.loads(
        (tmp_path / ".auto-agent" / "slices" / "auth" / "grill_answer.json").read_text()
    )
    assert "postgres" in answer["answer"]


# ---------------------------------------------------------------------------
# 2. User / standin is NEVER invoked during the parent-relay.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_clarification_not_invoked_during_relay(tmp_path: Path) -> None:
    """A sub-architect grill must NOT route through ``_emit_clarification``
    (which transitions the task to AWAITING_CLARIFICATION + publishes
    ARCHITECT_CLARIFICATION_NEEDED to the UI/PO).

    Asserted by patching ``_emit_clarification`` on the architect module
    and confirming the mock was never called.
    """

    from agent.lifecycle.trio import architect as architect_mod
    from agent.lifecycle.trio import sub_architect

    runner, _ = _make_relay_test_runner(tmp_path, question_rounds=1)

    async def fake_ask_parent(*, parent_task, slice_name, question, workspace_root):
        ans_path = (
            Path(workspace_root) / ".auto-agent" / "slices" / slice_name / "grill_answer.json"
        )
        ans_path.parent.mkdir(parents=True, exist_ok=True)
        ans_path.write_text(json.dumps({"schema_version": "1", "answer": "postgres"}))

    emit_clarification_mock = AsyncMock()

    with (
        patch.object(sub_architect, "_run_sub_architect_slice", runner),
        patch.object(sub_architect, "_ask_parent_to_answer_grill", fake_ask_parent),
        patch.object(architect_mod, "_emit_clarification", emit_clarification_mock),
    ):
        await sub_architect.dispatch_sub_architects(
            parent_task=_parent_stub(),
            workspace_root=str(tmp_path),
            slices=[{"name": "auth", "scope": "auth"}],
        )

    emit_clarification_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Multiple grill rounds in one sub-architect run.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_grill_rounds_round_trip_independently(tmp_path: Path) -> None:
    """If the sub-architect emits a grill question across multiple
    rounds, the orchestrator must resume the parent for each one.

    Pins that the parent-asker hook is called once per round, and the
    answer file is rewritten each time before the sub-architect
    resumes."""

    from agent.lifecycle.trio import sub_architect

    runner, state = _make_relay_test_runner(tmp_path, question_rounds=3)
    parent_invocations: list[str] = []

    async def fake_ask_parent(*, parent_task, slice_name, question, workspace_root):
        parent_invocations.append(question)
        ans_path = (
            Path(workspace_root) / ".auto-agent" / "slices" / slice_name / "grill_answer.json"
        )
        ans_path.parent.mkdir(parents=True, exist_ok=True)
        ans_path.write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "answer": f"answer to {len(parent_invocations)}",
                }
            )
        )

    with (
        patch.object(sub_architect, "_run_sub_architect_slice", runner),
        patch.object(sub_architect, "_ask_parent_to_answer_grill", fake_ask_parent),
    ):
        result = await sub_architect.dispatch_sub_architects(
            parent_task=_parent_stub(),
            workspace_root=str(tmp_path),
            slices=[{"name": "auth", "scope": "auth"}],
        )

    assert len(parent_invocations) == 3, (
        "parent must be re-asked for every grill question the sub-architect emits"
    )
    assert state["auth"] == 4, "sub-architect ran 3 grill rounds + 1 final completion"
    assert result.ok is True


# ---------------------------------------------------------------------------
# 4. Parent-grill-relay caps after enough rounds to avoid infinite loops.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grill_relay_caps_runaway_sub_architect(tmp_path: Path) -> None:
    """A sub-architect that never stops asking grills is bounded —
    the dispatcher gives up after a generous round cap and marks the
    slice failed so the parent task can escalate or block.
    """

    from agent.lifecycle.trio import sub_architect

    # Always paused for grill — never completes.
    async def relentless_runner(*, slice_root, slice_spec, **_kw):
        sp = Path(slice_root)
        sp.mkdir(parents=True, exist_ok=True)
        (sp / "grill_question.json").write_text(
            json.dumps({"schema_version": "1", "question": "and another thing?"})
        )
        return {"status": "paused_for_grill"}

    async def fake_ask_parent(*, parent_task, slice_name, question, workspace_root):
        ans_path = (
            Path(workspace_root) / ".auto-agent" / "slices" / slice_name / "grill_answer.json"
        )
        ans_path.parent.mkdir(parents=True, exist_ok=True)
        ans_path.write_text(json.dumps({"schema_version": "1", "answer": "ok"}))

    with (
        patch.object(sub_architect, "_run_sub_architect_slice", relentless_runner),
        patch.object(sub_architect, "_ask_parent_to_answer_grill", fake_ask_parent),
    ):
        result = await sub_architect.dispatch_sub_architects(
            parent_task=_parent_stub(),
            workspace_root=str(tmp_path),
            slices=[{"name": "auth", "scope": "auth"}],
        )

    assert result.ok is False
    auth = next(s for s in result.slice_results if s.name == "auth")
    assert auth.status == "failed"
    assert "grill" in (auth.reason or "").lower()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
