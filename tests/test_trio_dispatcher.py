"""Tests for :mod:`agent.lifecycle.trio.dispatcher`.

The dispatcher owns the coder↔reviewer round-trip for one backlog
item. Tests mock the subagent runners (``_run_coder`` / ``_run_reviewer``)
and the git helpers so we exercise the state machine without spinning
up real LLMs or a real git repo.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from agent.lifecycle.trio import dispatcher
from agent.lifecycle.trio.dispatcher import (
    ItemResult,
    TranscriptEntry,
    dispatch_item,
)


def _coder_entry(round_idx: int, *, summary: str = "did the thing") -> TranscriptEntry:
    return TranscriptEntry(role="coder", round=round_idx, output=summary)


def _reviewer_entry(
    round_idx: int, *, ok: bool, feedback: str = "", invalid: bool = False
) -> TranscriptEntry:
    verdict = None if invalid else {"ok": ok, "feedback": feedback}
    output = ("Looks good." if ok else f"Reject: {feedback}") + (
        ""
        if invalid
        else "\n\n```json\n"
        + ('{"ok": true, "feedback": ""}' if ok else f'{{"ok": false, "feedback": "{feedback}"}}')
        + "\n```"
    )
    return TranscriptEntry(role="reviewer", round=round_idx, output=output, verdict=verdict)


def _heavy_result(*, ok: bool, reason: str = ""):
    """Build a HeavyReviewResult shape for stubbing ``run_heavy_review``.

    ADR-015 §3 / Phase 7 — the dispatcher now drives the heavy reviewer
    instead of the old ``_run_reviewer`` boundary.
    """
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
async def test_first_round_ok_returns_done():
    """Happy path: coder writes code, heavy reviewer approves on round 1."""
    from agent.lifecycle.trio import reviewer as reviewer_mod

    with (
        patch.object(dispatcher, "_git_head_sha", AsyncMock(side_effect=["abc", "def"])),
        patch.object(dispatcher, "_git_diff_since", AsyncMock(return_value="+ new line\n")),
        patch.object(
            dispatcher,
            "_run_coder",
            AsyncMock(return_value=_coder_entry(1, summary="Added the fork.")),
        ),
        patch.object(
            reviewer_mod, "run_heavy_review", AsyncMock(return_value=_heavy_result(ok=True))
        ),
    ):
        result: ItemResult = await dispatch_item(**_KW)

    assert result.ok is True
    assert result.needs_tiebreak is False
    assert len(result.transcript) == 2  # coder + reviewer
    assert result.start_sha == "abc"
    assert result.head_sha == "def"


@pytest.mark.asyncio
async def test_second_round_ok_passes_feedback_back_to_coder():
    """Coder rejected on round 1, fixes on round 2, approved."""
    from agent.lifecycle.trio import reviewer as reviewer_mod

    run_coder = AsyncMock(side_effect=[_coder_entry(1), _coder_entry(2)])
    run_heavy = AsyncMock(
        side_effect=[
            _heavy_result(ok=False, reason="forgot to handle null"),
            _heavy_result(ok=True),
        ]
    )
    with (
        patch.object(dispatcher, "_git_head_sha", AsyncMock(side_effect=["s0", "s1"])),
        patch.object(dispatcher, "_git_diff_since", AsyncMock(return_value="+x")),
        patch.object(dispatcher, "_run_coder", run_coder),
        patch.object(reviewer_mod, "run_heavy_review", run_heavy),
    ):
        result = await dispatch_item(**_KW)

    assert result.ok is True
    assert len(result.transcript) == 4  # 2 coder + 2 reviewer entries
    # Round-2 coder must have seen the reviewer's feedback.
    second_call_kwargs = run_coder.await_args_list[1].kwargs
    assert "forgot to handle null" in second_call_kwargs["prior_feedback"]


@pytest.mark.asyncio
async def test_max_rounds_exhausted_returns_needs_tiebreak():
    """3 rejections in a row → ok=False, needs_tiebreak=True, no architect call."""
    from agent.lifecycle.trio import reviewer as reviewer_mod

    run_coder = AsyncMock(side_effect=[_coder_entry(i) for i in (1, 2, 3)])
    run_heavy = AsyncMock(
        side_effect=[
            _heavy_result(ok=False, reason="fb1"),
            _heavy_result(ok=False, reason="fb2"),
            _heavy_result(ok=False, reason="fb3"),
        ]
    )
    with (
        patch.object(dispatcher, "_git_head_sha", AsyncMock(side_effect=["s0", "s1"])),
        patch.object(dispatcher, "_git_diff_since", AsyncMock(return_value="+x")),
        patch.object(dispatcher, "_run_coder", run_coder),
        patch.object(reviewer_mod, "run_heavy_review", run_heavy),
    ):
        result = await dispatch_item(**_KW)

    assert result.ok is False
    assert result.needs_tiebreak is True
    assert run_coder.await_count == 3
    assert run_heavy.await_count == 3
    assert len(result.transcript) == 6


@pytest.mark.asyncio
async def test_coder_no_diff_re_prompts_then_fails():
    """If coder produces no diff at all, we re-prompt; final no-diff = terminal failure."""
    from agent.lifecycle.trio import reviewer as reviewer_mod

    run_coder = AsyncMock(side_effect=[_coder_entry(1), _coder_entry(2), _coder_entry(3)])
    run_heavy = AsyncMock()  # never called — diff is always empty
    with (
        patch.object(dispatcher, "_git_head_sha", AsyncMock(return_value="s0")),
        patch.object(dispatcher, "_git_diff_since", AsyncMock(return_value="   \n")),
        patch.object(dispatcher, "_run_coder", run_coder),
        patch.object(reviewer_mod, "run_heavy_review", run_heavy),
    ):
        result = await dispatch_item(**_KW)

    assert result.ok is False
    assert result.needs_tiebreak is False  # no point asking architect with no diff
    assert result.failure_reason == "coder_produced_no_diff"
    assert run_coder.await_count == 3
    run_heavy.assert_not_called()


@pytest.mark.asyncio
async def test_architect_tiebreak_extracts_decision():
    """The tiebreak runs the architect and parses the decision JSON."""
    from unittest.mock import MagicMock

    from agent.lifecycle.trio.dispatcher import architect_tiebreak

    architect_output = (
        "Read through the transcript. The coder is correct: the work item "
        "didn't ask for null-handling, the reviewer over-reached.\n\n"
        "```json\n"
        '{"action": "accept", "reason": "spec did not require null handling"}\n'
        "```"
    )
    fake_run_result = MagicMock(output=architect_output, tool_calls=[])
    fake_loop = MagicMock()
    fake_loop.run = AsyncMock(return_value=fake_run_result)
    fake_loop.tool_call_log = []

    transcript = [_coder_entry(1), _reviewer_entry(1, ok=False, feedback="needs null check")]

    with patch(
        "agent.lifecycle.trio.architect.create_architect_agent",
        return_value=fake_loop,
    ):
        decision = await architect_tiebreak(
            parent_task_id=170,
            work_item=_ITEM,
            transcript=transcript,
            workspace="/tmp/ws",
            repo_name=None,
            home_dir=None,
            org_id=1,
        )

    assert decision["action"] == "accept"
    assert "spec did not require null handling" in decision["reason"]


@pytest.mark.asyncio
async def test_architect_tiebreak_falls_back_to_clarify_on_bad_output():
    """No parseable decision → safe fallback to 'clarify' for human input."""
    from unittest.mock import MagicMock

    from agent.lifecycle.trio.dispatcher import architect_tiebreak

    fake_run_result = MagicMock(output="just prose, no JSON", tool_calls=[])
    fake_loop = MagicMock()
    fake_loop.run = AsyncMock(return_value=fake_run_result)
    fake_loop.tool_call_log = []

    with patch(
        "agent.lifecycle.trio.architect.create_architect_agent",
        return_value=fake_loop,
    ):
        decision = await architect_tiebreak(
            parent_task_id=170,
            work_item=_ITEM,
            transcript=[_coder_entry(1)],
            workspace="/tmp/ws",
            repo_name=None,
            home_dir=None,
            org_id=1,
        )

    assert decision["action"] == "clarify"


@pytest.mark.asyncio
async def test_invalid_reviewer_verdict_treated_as_reject():
    """Heavy reviewer emits a fail with empty reason → treated as reject, feedback explains."""
    from agent.lifecycle.trio import reviewer as reviewer_mod

    run_coder = AsyncMock(side_effect=[_coder_entry(1), _coder_entry(2)])
    # Round 1 fails with an empty reason → dispatcher synthesises a generic
    # feedback message so the coder still has something to act on.
    run_heavy = AsyncMock(
        side_effect=[
            _heavy_result(ok=False, reason=""),
            _heavy_result(ok=True),
        ]
    )
    with (
        patch.object(dispatcher, "_git_head_sha", AsyncMock(side_effect=["s0", "s1"])),
        patch.object(dispatcher, "_git_diff_since", AsyncMock(return_value="+x")),
        patch.object(dispatcher, "_run_coder", run_coder),
        patch.object(reviewer_mod, "run_heavy_review", run_heavy),
    ):
        result = await dispatch_item(**_KW)

    assert result.ok is True  # second round saved us
    second_call_kwargs = run_coder.await_args_list[1].kwargs
    # The dispatcher synthesises a default feedback when the reason is empty.
    assert second_call_kwargs["prior_feedback"], "coder must get some feedback"
