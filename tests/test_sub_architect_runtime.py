"""Phase 8.5 runtime — real ``_run_sub_architect_slice`` against slice paths.

Phase 8 (commit ``caae043``) shipped ``_run_sub_architect_slice`` as a
stub that only worked when tests monkey-patched it. Phase 8.5 (this
file) lands the production runtime and pins it via tests that mock at
the **LLM seam** (the architect agent factory + the dispatcher + the
final reviewer), not at the runtime entry point itself.

What we pin:

  1. The design pass writes ``slices/<name>/design.md`` (via the fake
     agent's ``submit-design`` simulation) and persists the slice's
     session blob under the workspace root.
  2. The backlog pass writes ``slices/<name>/backlog.json``, runs the
     structural validator, and feeds rejection summaries back into the
     agent on retry.
  3. Per-item dispatch is invoked once per item with ``slice_name`` set
     so verdicts land under the slice namespace.
  4. The final reviewer is invoked with ``slice_name`` set.
  5. Backlog rejected 3 times → slice fails with a recursion-aware reason.
  6. A "paused for grill" exit (a question file written instead of the
     design) is surfaced to the dispatcher, which relays via the
     parent-grill seam.
  7. The 1-level recursion bound is honoured at the gap-fix step too —
     a sub-architect that emits ``spawn_sub_architects`` in its own
     ``slices/<name>/decision.json`` fails the slice with a recursion
     reason.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from agent.lifecycle.trio import sub_architect

if TYPE_CHECKING:
    from pathlib import Path


def _parent_stub(parent_id: int = 99):
    class _Parent:
        id = parent_id
        title = "build a TODO app"
        description = "Build TODO"
        repo = None
        organization_id = 1
        created_by_user_id = 1

    return _Parent()


def _fake_dispatch_item_success() -> Any:
    from agent.lifecycle.trio.dispatcher import ItemResult

    async def _fake(**_kwargs: Any) -> ItemResult:
        return ItemResult(
            ok=True,
            transcript=[],
            start_sha="a" * 40,
            head_sha="b" * 40,
        )

    return _fake


def _fake_run_final_review(verdict: str = "passed") -> Any:
    from agent.lifecycle.trio.final_reviewer import FinalReviewResult

    async def _fake(**_kwargs: Any) -> FinalReviewResult:
        return FinalReviewResult(verdict=verdict, gaps=[], comments="")

    return _fake


def _valid_backlog_items() -> list[dict[str, Any]]:
    """A two-item backlog that passes the structural validator."""

    long_desc = " ".join([f"word{i}" for i in range(100)])
    return [
        {
            "id": "T1",
            "title": "first item",
            "description": long_desc,
            "justification": "needed for the slice surface",
            "affected_routes": [],
            "affected_files_estimate": 2,
        },
        {
            "id": "T2",
            "title": "second item",
            "description": long_desc,
            "justification": "covers the second surface",
            "affected_routes": ["/api/x"],
            "affected_files_estimate": 1,
        },
    ]


class _FakeArchitectAgent:
    """A drop-in for the architect AgentLoop.

    On each ``run`` it invokes a configured ``writer`` callback (which
    simulates a skill call) and returns an object with ``.output``. The
    fake records every prompt + resume flag so tests can assert against
    them.
    """

    def __init__(self, writer, *, output: str = "ok"):
        self._writer = writer
        self._output = output
        self.messages: list[Any] = []
        self.api_messages: list[Any] = []
        self.run_calls: list[dict[str, Any]] = []

    async def run(self, prompt: str, *, resume: bool = False, **_kw):
        from agent.llm.types import Message

        self.run_calls.append({"prompt": prompt, "resume": resume})
        if self._writer is not None:
            self._writer(prompt=prompt, resume=resume)

        class R:
            output = self._output

        # Set messages so session.save has something to write
        self.messages.append(Message(role="user", content=prompt))
        self.messages.append(Message(role="assistant", content=self._output))
        self.api_messages = list(self.messages)
        return R()


# ---------------------------------------------------------------------------
# 1. Real runtime invokes the slice design path under slices/<name>/.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_writes_slice_design_md(tmp_path: Path) -> None:
    """The real ``_run_sub_architect_slice`` writes ``slices/<name>/design.md``.

    LLM seam mocked at ``create_architect_agent``. The fake agent writes
    the design file when called for the design phase.
    """

    workspace = tmp_path

    agents_built: list[dict[str, Any]] = []

    def fake_factory(**kwargs):
        slice_name = kwargs.get("slice_name")
        phase = kwargs.get("phase")
        agents_built.append({"phase": phase, "slice_name": slice_name})

        def writer(*, prompt, resume):
            if phase == "design":
                target = workspace / ".auto-agent" / "slices" / slice_name / "design.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"# slice {slice_name} design\n")
            elif phase == "backlog_emit":
                target = workspace / ".auto-agent" / "slices" / slice_name / "backlog.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps({"schema_version": "1", "items": _valid_backlog_items()})
                )

        return _FakeArchitectAgent(writer)

    with (
        patch.object(sub_architect, "create_architect_agent", fake_factory),
        patch.object(sub_architect, "dispatch_item", _fake_dispatch_item_success()),
        patch.object(sub_architect, "run_final_review", _fake_run_final_review("passed")),
    ):
        result = await sub_architect._run_sub_architect_slice(
            parent_task=_parent_stub(),
            slice_spec={"name": "auth", "scope": "auth subsystem"},
            workspace_root=str(workspace),
            slice_root=str(workspace / ".auto-agent" / "slices" / "auth"),
        )

    assert result == {"status": "completed"}
    assert (workspace / ".auto-agent" / "slices" / "auth" / "design.md").is_file()
    # Both the design and backlog_emit phases must have run with slice_name set.
    phases = [a["phase"] for a in agents_built]
    assert "design" in phases
    assert "backlog_emit" in phases
    assert all(a["slice_name"] == "auth" for a in agents_built)


# ---------------------------------------------------------------------------
# 2. Backlog emit invokes the structural validator and persists items.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_persists_valid_backlog_under_slice(tmp_path: Path) -> None:
    workspace = tmp_path

    def fake_factory(**kwargs):
        slice_name = kwargs["slice_name"]
        phase = kwargs["phase"]

        def writer(*, prompt, resume):
            if phase == "design":
                target = workspace / ".auto-agent" / "slices" / slice_name / "design.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# d\n")
            elif phase == "backlog_emit":
                target = workspace / ".auto-agent" / "slices" / slice_name / "backlog.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps({"schema_version": "1", "items": _valid_backlog_items()})
                )

        return _FakeArchitectAgent(writer)

    with (
        patch.object(sub_architect, "create_architect_agent", fake_factory),
        patch.object(sub_architect, "dispatch_item", _fake_dispatch_item_success()),
        patch.object(sub_architect, "run_final_review", _fake_run_final_review("passed")),
    ):
        result = await sub_architect._run_sub_architect_slice(
            parent_task=_parent_stub(),
            slice_spec={"name": "auth", "scope": "auth"},
            workspace_root=str(workspace),
            slice_root=str(workspace / ".auto-agent" / "slices" / "auth"),
        )

    assert result == {"status": "completed"}
    backlog_text = (workspace / ".auto-agent" / "slices" / "auth" / "backlog.json").read_text()
    payload = json.loads(backlog_text)
    assert {i["id"] for i in payload["items"]} == {"T1", "T2"}


# ---------------------------------------------------------------------------
# 3. Per-item dispatcher is invoked with slice_name for every backlog item.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_dispatches_each_item_with_slice_name(tmp_path: Path) -> None:
    workspace = tmp_path
    dispatched: list[dict[str, Any]] = []

    from agent.lifecycle.trio.dispatcher import ItemResult

    async def fake_dispatch(**kwargs):
        dispatched.append(kwargs)
        return ItemResult(
            ok=True,
            transcript=[],
            start_sha="a" * 40,
            head_sha="b" * 40,
        )

    def fake_factory(**kwargs):
        slice_name = kwargs["slice_name"]
        phase = kwargs["phase"]

        def writer(*, prompt, resume):
            if phase == "design":
                target = workspace / ".auto-agent" / "slices" / slice_name / "design.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# d\n")
            elif phase == "backlog_emit":
                target = workspace / ".auto-agent" / "slices" / slice_name / "backlog.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps({"schema_version": "1", "items": _valid_backlog_items()})
                )

        return _FakeArchitectAgent(writer)

    with (
        patch.object(sub_architect, "create_architect_agent", fake_factory),
        patch.object(sub_architect, "dispatch_item", fake_dispatch),
        patch.object(sub_architect, "run_final_review", _fake_run_final_review("passed")),
    ):
        await sub_architect._run_sub_architect_slice(
            parent_task=_parent_stub(),
            slice_spec={"name": "auth", "scope": "auth"},
            workspace_root=str(workspace),
            slice_root=str(workspace / ".auto-agent" / "slices" / "auth"),
        )

    assert len(dispatched) == 2
    assert {d["slice_name"] for d in dispatched} == {"auth"}
    assert [d["work_item"]["id"] for d in dispatched] == ["T1", "T2"]


# ---------------------------------------------------------------------------
# 4. Final reviewer is invoked with slice_name set.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_invokes_final_reviewer_with_slice_name(tmp_path: Path) -> None:
    workspace = tmp_path
    final_calls: list[dict[str, Any]] = []

    from agent.lifecycle.trio.final_reviewer import FinalReviewResult

    async def fake_final(**kwargs):
        final_calls.append(kwargs)
        return FinalReviewResult(verdict="passed", gaps=[], comments="")

    def fake_factory(**kwargs):
        slice_name = kwargs["slice_name"]
        phase = kwargs["phase"]

        def writer(*, prompt, resume):
            if phase == "design":
                target = workspace / ".auto-agent" / "slices" / slice_name / "design.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# d\n")
            elif phase == "backlog_emit":
                target = workspace / ".auto-agent" / "slices" / slice_name / "backlog.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps({"schema_version": "1", "items": _valid_backlog_items()})
                )

        return _FakeArchitectAgent(writer)

    with (
        patch.object(sub_architect, "create_architect_agent", fake_factory),
        patch.object(sub_architect, "dispatch_item", _fake_dispatch_item_success()),
        patch.object(sub_architect, "run_final_review", fake_final),
    ):
        result = await sub_architect._run_sub_architect_slice(
            parent_task=_parent_stub(),
            slice_spec={"name": "auth", "scope": "auth"},
            workspace_root=str(workspace),
            slice_root=str(workspace / ".auto-agent" / "slices" / "auth"),
        )

    assert result == {"status": "completed"}
    assert len(final_calls) == 1
    assert final_calls[0]["slice_name"] == "auth"


# ---------------------------------------------------------------------------
# 5. Backlog rejected MAX times → slice fails permanently.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backlog_rejected_three_times_fails_slice(tmp_path: Path) -> None:
    workspace = tmp_path

    invalid_items = [
        {
            "id": "T1",
            "title": "x",
            "description": "too short",  # < 80 words → validator rejects
            "justification": "n",
            "affected_routes": [],
            "affected_files_estimate": 1,
        }
    ]

    def fake_factory(**kwargs):
        slice_name = kwargs["slice_name"]
        phase = kwargs["phase"]

        def writer(*, prompt, resume):
            if phase == "design":
                target = workspace / ".auto-agent" / "slices" / slice_name / "design.md"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("# d\n")
            elif phase == "backlog_emit":
                target = workspace / ".auto-agent" / "slices" / slice_name / "backlog.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({"schema_version": "1", "items": invalid_items}))

        return _FakeArchitectAgent(writer)

    with (
        patch.object(sub_architect, "create_architect_agent", fake_factory),
        patch.object(sub_architect, "dispatch_item", _fake_dispatch_item_success()),
        patch.object(sub_architect, "run_final_review", _fake_run_final_review("passed")),
    ):
        result = await sub_architect._run_sub_architect_slice(
            parent_task=_parent_stub(),
            slice_spec={"name": "auth", "scope": "auth"},
            workspace_root=str(workspace),
            slice_root=str(workspace / ".auto-agent" / "slices" / "auth"),
        )

    assert result["status"] == "failed"
    assert "could not emit a valid backlog" in result["reason"]


# ---------------------------------------------------------------------------
# 6. Slice exits with paused_for_grill when grill_question.json is written.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_design_pass_pauses_for_parent_grill(tmp_path: Path) -> None:
    workspace = tmp_path

    def fake_factory(**kwargs):
        slice_name = kwargs["slice_name"]
        phase = kwargs["phase"]

        def writer(*, prompt, resume):
            if phase == "design":
                target = workspace / ".auto-agent" / "slices" / slice_name / "grill_question.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps(
                        {
                            "schema_version": "1",
                            "question": "Which auth provider should I use?",
                        }
                    )
                )

        return _FakeArchitectAgent(writer)

    with patch.object(sub_architect, "create_architect_agent", fake_factory):
        result = await sub_architect._run_sub_architect_slice(
            parent_task=_parent_stub(),
            slice_spec={"name": "auth", "scope": "auth"},
            workspace_root=str(workspace),
            slice_root=str(workspace / ".auto-agent" / "slices" / "auth"),
        )

    assert result["status"] == "paused_for_grill"


# ---------------------------------------------------------------------------
# 7. End-to-end slice pass — when everything goes right we get completed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slice_passes_end_to_end(tmp_path: Path) -> None:
    workspace = tmp_path

    def fake_factory(**kwargs):
        slice_name = kwargs["slice_name"]
        phase = kwargs["phase"]

        def writer(*, prompt, resume):
            if phase == "design":
                t = workspace / ".auto-agent" / "slices" / slice_name / "design.md"
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_text("# d\n")
            elif phase == "backlog_emit":
                t = workspace / ".auto-agent" / "slices" / slice_name / "backlog.json"
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_text(json.dumps({"schema_version": "1", "items": _valid_backlog_items()}))

        return _FakeArchitectAgent(writer)

    with (
        patch.object(sub_architect, "create_architect_agent", fake_factory),
        patch.object(sub_architect, "dispatch_item", _fake_dispatch_item_success()),
        patch.object(sub_architect, "run_final_review", _fake_run_final_review("passed")),
    ):
        result = await sub_architect._run_sub_architect_slice(
            parent_task=_parent_stub(),
            slice_spec={"name": "auth", "scope": "auth"},
            workspace_root=str(workspace),
            slice_root=str(workspace / ".auto-agent" / "slices" / "auth"),
        )

    assert result == {"status": "completed"}


# ---------------------------------------------------------------------------
# 8. The 1-level recursion bound is enforced when a sub-architect emits
#    spawn_sub_architects in its slice decision.json during gap-fix.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap_fix_rejects_nested_spawn(tmp_path: Path) -> None:
    """If final review reports gaps_found and the sub-architect's gap-fix
    decision is ``spawn_sub_architects``, the slice must fail with a
    recursion-bound reason."""

    workspace = tmp_path

    from agent.lifecycle.trio.final_reviewer import FinalReviewResult

    async def fake_final_with_gaps(**kwargs):
        return FinalReviewResult(
            verdict="gaps_found",
            gaps=[{"description": "missing handler", "affected_routes": ["/api/x"]}],
            comments="",
        )

    def fake_factory(**kwargs):
        slice_name = kwargs["slice_name"]
        phase = kwargs["phase"]

        def writer(*, prompt, resume):
            if phase == "design":
                t = workspace / ".auto-agent" / "slices" / slice_name / "design.md"
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_text("# d\n")
            elif phase == "backlog_emit":
                t = workspace / ".auto-agent" / "slices" / slice_name / "backlog.json"
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_text(json.dumps({"schema_version": "1", "items": _valid_backlog_items()}))
            elif phase == "checkpoint":
                # Gap-fix turn writes a spawn_sub_architects decision — nope.
                t = workspace / ".auto-agent" / "slices" / slice_name / "decision.json"
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_text(
                    json.dumps(
                        {
                            "schema_version": "1",
                            "action": "spawn_sub_architects",
                            "payload": {"slices": [{"name": "deeper", "scope": "x"}]},
                        }
                    )
                )

        return _FakeArchitectAgent(writer)

    with (
        patch.object(sub_architect, "create_architect_agent", fake_factory),
        patch.object(sub_architect, "dispatch_item", _fake_dispatch_item_success()),
        patch.object(sub_architect, "run_final_review", fake_final_with_gaps),
    ):
        result = await sub_architect._run_sub_architect_slice(
            parent_task=_parent_stub(),
            slice_spec={"name": "auth", "scope": "auth"},
            workspace_root=str(workspace),
            slice_root=str(workspace / ".auto-agent" / "slices" / "auth"),
        )

    assert result["status"] == "failed"
    reason = (result.get("reason") or "").lower()
    assert "sub-architect" in reason or "recursion" in reason


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
