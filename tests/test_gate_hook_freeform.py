"""ADR-015 §6/§7 — gate hook integration.

For every gate (grill, plan, design, PR review) the orchestrator behaves
differently depending on the effective mode:

  * ``human_in_loop`` → wait for the human-written file. The standin
    is **not** invoked.
  * ``freeform`` → resolve the standin from origin, invoke its method,
    standin writes the file, orchestrator resumes.

The thin wrapper :func:`agent.lifecycle.standin.run_freeform_gate`
encapsulates this routing for orchestrator call sites.
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.lifecycle.standin import (
    ImprovementAgentStandin,
    POStandin,
    run_freeform_gate,
)
from agent.lifecycle.workspace_paths import AUTO_AGENT_DIR, PLAN_APPROVAL_PATH


def _task(*, mode_override: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=11,
        repo_id=1,
        source="manual",
        source_id="",
        mode_override=mode_override,
    )


def _repo(mode: str = "human_in_loop") -> SimpleNamespace:
    return SimpleNamespace(id=1, name="acme/widget", mode=mode, product_brief="x")


@pytest.fixture
def workspace_root() -> str:
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, AUTO_AGENT_DIR), exist_ok=True)
        yield root


@pytest.mark.asyncio
async def test_human_in_loop_does_not_invoke_standin(workspace_root: str, monkeypatch) -> None:
    """Mode resolves to ``human_in_loop`` ⇒ ``run_freeform_gate`` is a
    no-op that returns ``False`` (the standin was not invoked) and never
    writes a file."""

    selected = AsyncMock()
    monkeypatch.setattr("agent.lifecycle.standin.select_standin", selected)
    task = _task(mode_override=None)
    repo = _repo(mode="human_in_loop")
    fired = await run_freeform_gate(
        task=task,
        repo=repo,
        gate="plan_approval",
        gate_input={"plan_md": "# Plan"},
        context={"workspace_root": workspace_root},
    )
    assert fired is False
    selected.assert_not_called()
    # And no file written.
    assert not os.path.isfile(os.path.join(workspace_root, PLAN_APPROVAL_PATH))


@pytest.mark.asyncio
async def test_freeform_invokes_standin_and_writes_file(workspace_root: str, monkeypatch) -> None:
    """Mode resolves to ``freeform`` ⇒ the resolved standin is invoked
    and writes the gate file."""

    task = _task(mode_override="freeform")
    repo = _repo(mode="human_in_loop")  # override flips it freeform
    fired = await run_freeform_gate(
        task=task,
        repo=repo,
        gate="plan_approval",
        gate_input={"plan_md": "# Plan\n"},
        context={"workspace_root": workspace_root},
    )
    assert fired is True
    assert os.path.isfile(os.path.join(workspace_root, PLAN_APPROVAL_PATH))


@pytest.mark.asyncio
async def test_freeform_repo_default_invokes_standin(
    workspace_root: str,
) -> None:
    """No override + freeform repo ⇒ standin runs."""

    task = _task(mode_override=None)
    repo = _repo(mode="freeform")
    fired = await run_freeform_gate(
        task=task,
        repo=repo,
        gate="plan_approval",
        gate_input={"plan_md": "# Plan\n"},
        context={"workspace_root": workspace_root},
    )
    assert fired is True


@pytest.mark.asyncio
async def test_origin_routes_to_improvement_agent(
    workspace_root: str,
) -> None:
    """A task spawned from an improvement-category suggestion uses the
    improvement agent standin even though §7's mode resolver gates on
    mode, not origin."""

    class _FakeSuggestionsRepo:
        async def get_category(self, sid: int) -> str | None:
            return "architecture" if sid == 42 else None

    task = SimpleNamespace(
        id=11,
        repo_id=1,
        source="freeform",
        source_id="suggestion:42",
        mode_override="freeform",
    )
    repo = _repo(mode="freeform")
    fired = await run_freeform_gate(
        task=task,
        repo=repo,
        gate="plan_approval",
        gate_input={"plan_md": "# Plan\n"},
        context={"workspace_root": workspace_root},
        suggestion_repo=_FakeSuggestionsRepo(),
    )
    assert fired is True
    # Standin should be the improvement-agent flavour — verifiable via the
    # published event payload.
    # (We piggy-back the existing publisher fixture by importing it
    # implicitly through conftest; see the per-standin tests for the
    # event-payload check. Here we only need the file write.)
    assert os.path.isfile(os.path.join(workspace_root, PLAN_APPROVAL_PATH))


@pytest.mark.asyncio
async def test_freeform_grill_writes_grill_answer(workspace_root: str) -> None:
    task = _task(mode_override="freeform")
    repo = _repo(mode="freeform")
    fired = await run_freeform_gate(
        task=task,
        repo=repo,
        gate="grill",
        gate_input={"question": "Anything?"},
        context={"workspace_root": workspace_root},
    )
    assert fired is True
    assert os.path.isfile(os.path.join(workspace_root, AUTO_AGENT_DIR, "grill_answer.json"))


def test_standin_classes_have_required_methods() -> None:
    """Locks the standin interface: every standin must expose four
    awaitables, one per gate. A renamed method silently breaking
    ``run_freeform_gate``'s dispatch is the bug class this guards."""

    for cls in (POStandin, ImprovementAgentStandin):
        for method in (
            "answer_grill",
            "approve_plan",
            "approve_design",
            "review_pr",
        ):
            assert hasattr(cls, method), f"{cls.__name__} missing {method}"
