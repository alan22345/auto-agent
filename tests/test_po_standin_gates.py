"""ADR-015 §6 — PO standin writes the right gate files in freeform.

Each standin method must:

  * write the canonical gate file (``grill_answer.json`` /
    ``plan_approval.json`` / ``pr_review.json``) at the path the
    orchestrator polls.
  * never escape to the user — picks a sensible default when context is
    missing and logs ``fallback_default(source=heuristic)``.
  * publish a structured ``gate_decision`` event so the audit trail
    can reconstruct who decided what at every gate.

These tests stub the LLM-driven decision so they don't depend on
network / model availability. The standin code path *under the stub* is
what gates the file write — that's the contract we pin.
"""

from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

import pytest

from agent.lifecycle.standin import POStandin
from agent.lifecycle.workspace_paths import (
    AUTO_AGENT_DIR,
    PLAN_APPROVAL_PATH,
    PR_REVIEW_PATH,
)


def _make_task(repo_id: int = 1, task_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        repo_id=repo_id,
        source="manual",
        source_id="",
        mode_override="freeform",
    )


def _make_repo(product_brief: str = "Product: a dashboard.") -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        name="acme/widget",
        mode="freeform",
        product_brief=product_brief,
    )


@pytest.fixture
def workspace_root() -> str:
    """Per-test workspace dir — gate files land under here."""

    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, AUTO_AGENT_DIR), exist_ok=True)
        yield root


@pytest.mark.asyncio
async def test_answer_grill_writes_grill_answer_file(workspace_root: str, publisher) -> None:
    standin = POStandin(task=_make_task(), repo=_make_repo())
    await standin.answer_grill(
        question="Should the dashboard support dark mode?",
        context={"workspace_root": workspace_root},
    )
    # PO standin writes the grill_answer.json file the orchestrator polls.
    path = os.path.join(workspace_root, AUTO_AGENT_DIR, "grill_answer.json")
    assert os.path.isfile(path), "grill_answer.json was not written"
    with open(path) as fh:
        payload = json.load(fh)
    assert payload.get("schema_version") == "1"
    assert "answer" in payload


@pytest.mark.asyncio
async def test_approve_plan_writes_approval_file(workspace_root: str, publisher) -> None:
    standin = POStandin(task=_make_task(), repo=_make_repo())
    await standin.approve_plan(
        plan_md="# Plan\n\n- step 1\n- step 2",
        context={"workspace_root": workspace_root},
    )
    path = os.path.join(workspace_root, PLAN_APPROVAL_PATH)
    assert os.path.isfile(path), "plan_approval.json was not written"
    with open(path) as fh:
        payload = json.load(fh)
    assert payload.get("verdict") in {"approved", "rejected"}
    assert payload.get("schema_version") == "1"


@pytest.mark.asyncio
async def test_approve_design_writes_approval_file(workspace_root: str, publisher) -> None:
    standin = POStandin(task=_make_task(), repo=_make_repo())
    await standin.approve_design(
        design_md="# Design\n\nOverview.",
        context={"workspace_root": workspace_root},
    )
    # Design approval reuses the plan_approval.json contract per ADR-015 §2.
    path = os.path.join(workspace_root, PLAN_APPROVAL_PATH)
    assert os.path.isfile(path)
    with open(path) as fh:
        payload = json.load(fh)
    assert payload.get("verdict") in {"approved", "rejected"}


@pytest.mark.asyncio
async def test_review_pr_writes_pr_review_file(workspace_root: str, publisher) -> None:
    standin = POStandin(task=_make_task(), repo=_make_repo())
    await standin.review_pr(
        pr_diff="diff --git a/x b/x\n+foo\n",
        pr_metadata={"title": "Add foo", "url": "https://x"},
        context={"workspace_root": workspace_root},
    )
    path = os.path.join(workspace_root, PR_REVIEW_PATH)
    assert os.path.isfile(path), "pr_review.json was not written"
    with open(path) as fh:
        payload = json.load(fh)
    assert payload.get("verdict") in {"approved", "changes_requested"}
    assert payload.get("schema_version") == "1"


@pytest.mark.asyncio
async def test_empty_product_brief_logs_fallback_default(workspace_root: str, publisher) -> None:
    """No product_brief ⇒ standin still picks a default and surfaces a
    ``fallback_reasons`` entry on the decision event. Never escapes."""

    standin = POStandin(task=_make_task(), repo=_make_repo(product_brief=""))
    await standin.answer_grill(
        question="Anything?",
        context={"workspace_root": workspace_root},
    )
    # Gate file written — never escapes.
    assert os.path.isfile(os.path.join(workspace_root, AUTO_AGENT_DIR, "grill_answer.json"))
    # And the structured fallback marker rides on the decision event so
    # consumers don't have to grep log lines. Per ADR-015 §6 the marker
    # is "fallback_default(source=heuristic)" — we encode this as a
    # gate:reason string on the event.
    matching = [ev for ev in publisher.events if str(ev.type) == "standin.decision"]
    assert matching
    fallbacks = matching[-1].payload.get("fallback_reasons", [])
    assert any(r == "grill:no_product_brief" for r in fallbacks)


@pytest.mark.asyncio
async def test_decision_event_published(workspace_root: str, publisher) -> None:
    """Every standin decision publishes a structured ``standin.decision``
    event with standin_kind / agent_id / gate / task_id, so the gate
    history can reconstruct who decided what."""

    standin = POStandin(task=_make_task(task_id=42), repo=_make_repo())
    await standin.approve_plan(
        plan_md="# Plan",
        context={"workspace_root": workspace_root},
    )
    matching = [ev for ev in publisher.events if str(ev.type).startswith("standin.")]
    assert matching, "no standin.* event was published"
    ev = matching[-1]
    payload = ev.payload
    assert payload.get("standin_kind") == "po"
    assert payload.get("gate") == "plan_approval"
    assert payload.get("task_id") == 42
    assert "decision" in payload
