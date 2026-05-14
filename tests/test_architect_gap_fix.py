"""Architect gap-fix loop — ADR-015 §4 / Phase 7.

When the final reviewer returns ``gaps_found``, the orchestrator
resumes the architect's persisted session (Phase 6 wired persistence)
and prompts: "the final reviewer found these gaps: ...". The architect
emits a fresh ``decision.json`` via ``submit-architect-decision`` —
typically ``action="dispatch_new"`` with new backlog items — and the
orchestrator dispatches them through the normal builder → heavy-review
loop. Bound: 3 rounds. 4th round → BLOCKED.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle.trio import gap_fix

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_gap_fix_resumes_persisted_session(tmp_path: Path) -> None:
    """The architect's persisted Session is loaded and resume=True passed in."""

    workspace = tmp_path

    captured_resume: list[bool] = []
    captured_session: list[object] = []

    class _Agent:
        async def run(self, prompt, *, resume=False, **_kw):
            captured_resume.append(resume)
            (workspace / ".auto-agent").mkdir(exist_ok=True)
            (workspace / ".auto-agent" / "decision.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "action": "dispatch_new",
                        "payload": {
                            "items": [
                                {
                                    "id": "G1",
                                    "title": "fix gap",
                                    "description": "fixes the discovered gap",
                                    "affected_routes": ["/api/a"],
                                }
                            ]
                        },
                    }
                )
            )

            class R:
                output = "wrote decision.json"

            return R()

    def fake_create_arch_agent(*, session=None, **_kw):
        captured_session.append(session)
        return _Agent()

    fake_session = object()
    with (
        patch.object(gap_fix, "_load_architect_session", AsyncMock(return_value=fake_session)),
        patch.object(gap_fix, "create_architect_agent", fake_create_arch_agent),
        patch.object(gap_fix, "_prepare_parent_workspace", AsyncMock(return_value=str(workspace))),
        patch.object(
            gap_fix,
            "_load_parent_for_run",
            AsyncMock(
                return_value={
                    "task_description": "x",
                    "task_title": "T",
                    "repo_name": None,
                    "org_id": 1,
                    "home_dir": None,
                    "__parent": None,
                }
            ),
        ),
    ):
        decision = await gap_fix.run_gap_fix(
            parent_task_id=42,
            gaps=[{"description": "missing handler", "affected_routes": ["/api/a"]}],
            round_idx=1,
        )

    assert decision["action"] == "dispatch_new"
    assert captured_session == [fake_session], "architect session was not resumed"
    assert any(captured_resume), "agent.run must be called with resume=True"
    assert decision.get("items") or decision.get("payload", {}).get("items")


@pytest.mark.asyncio
async def test_gap_fix_emits_new_items_via_skill(tmp_path: Path) -> None:
    """Architect writes decision.json with action=dispatch_new + new backlog items."""

    workspace = tmp_path

    class _Agent:
        async def run(self, prompt, *, resume=False, **_kw):
            (workspace / ".auto-agent").mkdir(exist_ok=True)
            (workspace / ".auto-agent" / "decision.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1",
                        "action": "dispatch_new",
                        "payload": {
                            "items": [
                                {
                                    "id": "G1",
                                    "title": "gap1",
                                    "description": "x",
                                    "affected_routes": [],
                                },
                                {
                                    "id": "G2",
                                    "title": "gap2",
                                    "description": "y",
                                    "affected_routes": ["/foo"],
                                },
                            ]
                        },
                    }
                )
            )

            class R:
                output = ""

            return R()

    with (
        patch.object(gap_fix, "_load_architect_session", AsyncMock(return_value=object())),
        patch.object(gap_fix, "create_architect_agent", lambda **kw: _Agent()),
        patch.object(gap_fix, "_prepare_parent_workspace", AsyncMock(return_value=str(workspace))),
        patch.object(
            gap_fix,
            "_load_parent_for_run",
            AsyncMock(
                return_value={
                    "task_description": "x",
                    "task_title": "T",
                    "repo_name": None,
                    "org_id": 1,
                    "home_dir": None,
                    "__parent": None,
                }
            ),
        ),
    ):
        decision = await gap_fix.run_gap_fix(
            parent_task_id=1,
            gaps=[{"description": "g", "affected_routes": []}],
            round_idx=1,
        )

    new_items = decision.get("items") or decision.get("payload", {}).get("items")
    assert len(new_items) == 2
    assert {i["id"] for i in new_items} == {"G1", "G2"}


@pytest.mark.asyncio
async def test_gap_fix_round_bound_blocks_after_three_rounds(tmp_path: Path) -> None:
    """4th gap-fix round → returns a BLOCKED decision and does NOT call agent.run."""

    workspace = tmp_path
    called = {"count": 0}

    class _Agent:
        async def run(self, prompt, *, resume=False, **_kw):
            called["count"] += 1

            class R:
                output = ""

            return R()

    with (
        patch.object(gap_fix, "_load_architect_session", AsyncMock(return_value=object())),
        patch.object(gap_fix, "create_architect_agent", lambda **kw: _Agent()),
        patch.object(gap_fix, "_prepare_parent_workspace", AsyncMock(return_value=str(workspace))),
        patch.object(
            gap_fix,
            "_load_parent_for_run",
            AsyncMock(
                return_value={
                    "task_description": "x",
                    "task_title": "T",
                    "repo_name": None,
                    "org_id": 1,
                    "home_dir": None,
                    "__parent": None,
                }
            ),
        ),
    ):
        decision = await gap_fix.run_gap_fix(
            parent_task_id=1,
            gaps=[{"description": "g", "affected_routes": []}],
            round_idx=4,
        )

    assert decision["action"] in ("blocked", "escalate")
    assert called["count"] == 0, "agent must not be invoked once the bound is exceeded"
