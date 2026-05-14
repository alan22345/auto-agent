"""ADR-015 §6 — Improvement-agent standin gates.

Sibling spec to ``test_po_standin_gates.py`` — the improvement agent
standin must produce gate files of the same shape, with
``standin_kind="improvement_agent"`` on the published event.
"""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

import pytest

from agent.lifecycle.standin import ImprovementAgentStandin
from agent.lifecycle.workspace_paths import (
    AUTO_AGENT_DIR,
    PLAN_APPROVAL_PATH,
    PR_REVIEW_PATH,
)


def _make_task(task_id: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        repo_id=1,
        source="freeform",
        source_id="suggestion:1",
        mode_override="freeform",
    )


def _make_repo() -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        name="acme/widget",
        mode="freeform",
        product_brief="Internal admin dashboard.",
    )


@pytest.fixture
def workspace_root() -> str:
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, AUTO_AGENT_DIR), exist_ok=True)
        yield root


@pytest.mark.asyncio
async def test_improvement_answer_grill_writes_file(workspace_root: str, publisher) -> None:
    standin = ImprovementAgentStandin(task=_make_task(), repo=_make_repo())
    await standin.answer_grill(
        question="Where should the auth-layer cache live?",
        context={"workspace_root": workspace_root},
    )
    path = os.path.join(workspace_root, AUTO_AGENT_DIR, "grill_answer.json")
    assert os.path.isfile(path)
    with open(path) as fh:
        payload = json.load(fh)
    assert "answer" in payload
    assert payload.get("schema_version") == "1"


@pytest.mark.asyncio
async def test_improvement_approve_plan_writes_file(workspace_root: str, publisher) -> None:
    standin = ImprovementAgentStandin(task=_make_task(), repo=_make_repo())
    await standin.approve_plan(
        plan_md="# Plan\n\n- deepen module foo",
        context={"workspace_root": workspace_root},
    )
    path = os.path.join(workspace_root, PLAN_APPROVAL_PATH)
    assert os.path.isfile(path)
    with open(path) as fh:
        payload = json.load(fh)
    assert payload.get("verdict") in {"approved", "rejected"}


@pytest.mark.asyncio
async def test_improvement_approve_design_writes_file(workspace_root: str, publisher) -> None:
    standin = ImprovementAgentStandin(task=_make_task(), repo=_make_repo())
    await standin.approve_design(
        design_md="# Design",
        context={"workspace_root": workspace_root},
    )
    path = os.path.join(workspace_root, PLAN_APPROVAL_PATH)
    assert os.path.isfile(path)


@pytest.mark.asyncio
async def test_improvement_review_pr_writes_file(workspace_root: str, publisher) -> None:
    standin = ImprovementAgentStandin(task=_make_task(), repo=_make_repo())
    await standin.review_pr(
        pr_diff="diff --git a/y b/y\n",
        pr_metadata={"title": "Refactor"},
        context={"workspace_root": workspace_root},
    )
    path = os.path.join(workspace_root, PR_REVIEW_PATH)
    assert os.path.isfile(path)


@pytest.mark.asyncio
async def test_improvement_decision_event_kind(workspace_root: str, publisher) -> None:
    standin = ImprovementAgentStandin(task=_make_task(task_id=99), repo=_make_repo())
    await standin.review_pr(
        pr_diff="diff",
        pr_metadata={},
        context={"workspace_root": workspace_root},
    )
    matching = [ev for ev in publisher.events if str(ev.type).startswith("standin.")]
    assert matching
    ev = matching[-1]
    assert ev.payload.get("standin_kind") == "improvement_agent"
    assert ev.payload.get("gate") == "pr_review"
    assert ev.payload.get("task_id") == 99
