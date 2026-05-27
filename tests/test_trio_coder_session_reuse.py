"""Phase 5 — verify the trio coder reuses its session across rounds.

The dispatcher's docstring claims: "feed the feedback back into the SAME
coder on the next round (resume=True)". But _run_coder put round_idx into
the session_id AND never passed resume=True to agent.run, so every round
was a fresh claude --print session. This test pins the fixed behavior:

- One stable session_id per (parent_task_id, item_id) across all rounds.
- resume=False on round 1, resume=True on rounds 2+.

The reviewer is intentionally NOT touched — it stays fresh per round for
independence.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle.trio import dispatcher
from agent.lifecycle.trio.dispatcher import dispatch_item


def _heavy_result(*, ok: bool, reason: str = ""):
    from agent.lifecycle.trio.reviewer import HeavyReviewResult

    return HeavyReviewResult(
        verdict="pass" if ok else "fail",
        alignment="aligned",
        smoke="ok",
        ui="n/a",
        reason=reason or ("pass" if ok else "rejected"),
    )


_ITEM = {"id": "cf-1", "title": "Backend fork", "description": "Implement the fork."}
_KW = dict(
    parent_task_id=170,
    work_item=_ITEM,
    workspace="/tmp/ws",
    repo_name="repo",
    home_dir=None,
    org_id=1,
)


@pytest.mark.asyncio
async def test_coder_reuses_session_id_across_rounds_and_resumes_after_round_one():
    """All three coder rounds for one item share a session_id; resume=True on rounds 2+."""

    from agent.lifecycle.trio import reviewer as reviewer_mod

    captured_create_kwargs: list[dict] = []
    captured_run_calls: list[dict] = []

    def fake_create_agent(**kwargs):
        captured_create_kwargs.append(kwargs)

        async def fake_run(prompt, system=None, resume=False):
            captured_run_calls.append({"resume": resume})
            return SimpleNamespace(output="coder ran", tool_calls=[])

        return SimpleNamespace(run=fake_run, tool_call_log=[])

    # Heavy reviewer rejects twice, then passes — drives 3 coder rounds.
    heavy_results = [
        _heavy_result(ok=False, reason="fix x"),
        _heavy_result(ok=False, reason="fix y"),
        _heavy_result(ok=True),
    ]

    with (
        patch.object(dispatcher, "_git_head_sha", AsyncMock(side_effect=["abc", "ghi"])),
        patch.object(dispatcher, "_git_diff_since", AsyncMock(return_value="+ change\n")),
        patch.object(dispatcher, "create_agent", side_effect=fake_create_agent),
        patch.object(reviewer_mod, "run_heavy_review", AsyncMock(side_effect=heavy_results)),
    ):
        result = await dispatch_item(**_KW)

    assert result.ok is True, "third round should have passed"
    assert len(captured_create_kwargs) == 3, "expected one create_agent call per coder round"

    session_ids = [k.get("session_id") for k in captured_create_kwargs]
    assert all(session_ids), "every coder round must allocate a session_id"
    assert len(set(session_ids)) == 1, (
        f"coder session_id must be stable across rounds, got {session_ids}"
    )
    # Must be a valid UUID — Claude CLI rejects --session-id <non-uuid> with
    # an instant error, which would silently return [ERROR CLI exited N] as
    # the coder's "result" and trip coder_produced_no_diff (task 28, 2026-05-27).
    import uuid as _uuid

    _uuid.UUID(session_ids[0])  # raises if not a valid UUID
    # Same (parent, item) → same UUID; different inputs → different UUID.
    from agent.lifecycle.trio.dispatcher import _coder_session_id

    assert session_ids[0] == _coder_session_id(170, "cf-1")
    assert _coder_session_id(170, "cf-1") != _coder_session_id(170, "cf-2")
    assert _coder_session_id(170, "cf-1") != _coder_session_id(171, "cf-1")

    assert captured_run_calls[0]["resume"] is False, "round 1 starts a fresh session"
    assert captured_run_calls[1]["resume"] is True, "round 2 must --resume"
    assert captured_run_calls[2]["resume"] is True, "round 3 must --resume"


@pytest.mark.asyncio
async def test_coder_resumes_even_on_no_diff_retry():
    """The no-diff retry path (round 1 produces no diff, round 2 retries) must also resume."""

    from agent.lifecycle.trio import reviewer as reviewer_mod

    captured_run_calls: list[dict] = []

    def fake_create_agent(**kwargs):
        async def fake_run(prompt, system=None, resume=False):
            captured_run_calls.append({"resume": resume})
            return SimpleNamespace(output="coder ran", tool_calls=[])

        return SimpleNamespace(run=fake_run, tool_call_log=[])

    # Round 1: no diff (triggers retry-with-feedback without invoking reviewer).
    # Round 2: real diff → reviewer passes.
    with (
        patch.object(dispatcher, "_git_head_sha", AsyncMock(side_effect=["abc", "ghi"])),
        patch.object(
            dispatcher,
            "_git_diff_since",
            AsyncMock(side_effect=["", "+ real change\n"]),
        ),
        patch.object(dispatcher, "create_agent", side_effect=fake_create_agent),
        patch.object(
            reviewer_mod, "run_heavy_review", AsyncMock(return_value=_heavy_result(ok=True))
        ),
    ):
        result = await dispatch_item(**_KW)

    assert result.ok is True
    assert len(captured_run_calls) == 2
    assert captured_run_calls[0]["resume"] is False
    assert captured_run_calls[1]["resume"] is True, (
        "no-diff retry must still --resume the same coder session"
    )
