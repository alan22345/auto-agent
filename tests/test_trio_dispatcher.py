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
    output = (
        "Looks good." if ok else f"Reject: {feedback}"
    ) + (
        ""
        if invalid
        else "\n\n```json\n"
        + ('{"ok": true, "feedback": ""}' if ok else f'{{"ok": false, "feedback": "{feedback}"}}')
        + "\n```"
    )
    return TranscriptEntry(
        role="reviewer", round=round_idx, output=output, verdict=verdict
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
    """Happy path: coder writes code, reviewer approves on round 1."""
    with (
        patch.object(dispatcher, "_git_head_sha", AsyncMock(side_effect=["abc", "def"])),
        patch.object(dispatcher, "_git_diff_since", AsyncMock(return_value="+ new line\n")),
        patch.object(
            dispatcher, "_run_coder",
            AsyncMock(return_value=_coder_entry(1, summary="Added the fork."))
        ),
        patch.object(
            dispatcher, "_run_reviewer",
            AsyncMock(return_value=_reviewer_entry(1, ok=True))
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
    run_coder = AsyncMock(side_effect=[_coder_entry(1), _coder_entry(2)])
    run_reviewer = AsyncMock(
        side_effect=[
            _reviewer_entry(1, ok=False, feedback="forgot to handle null"),
            _reviewer_entry(2, ok=True),
        ]
    )
    with (
        patch.object(dispatcher, "_git_head_sha", AsyncMock(side_effect=["s0", "s1"])),
        patch.object(dispatcher, "_git_diff_since", AsyncMock(return_value="+x")),
        patch.object(dispatcher, "_run_coder", run_coder),
        patch.object(dispatcher, "_run_reviewer", run_reviewer),
    ):
        result = await dispatch_item(**_KW)

    assert result.ok is True
    assert len(result.transcript) == 4  # 2 coder + 2 reviewer
    # Round-2 coder must have seen the reviewer's feedback.
    second_call_kwargs = run_coder.await_args_list[1].kwargs
    assert "forgot to handle null" in second_call_kwargs["prior_feedback"]


@pytest.mark.asyncio
async def test_max_rounds_exhausted_returns_needs_tiebreak():
    """3 rejections in a row → ok=False, needs_tiebreak=True, no architect call."""
    run_coder = AsyncMock(side_effect=[_coder_entry(i) for i in (1, 2, 3)])
    run_reviewer = AsyncMock(
        side_effect=[
            _reviewer_entry(1, ok=False, feedback="fb1"),
            _reviewer_entry(2, ok=False, feedback="fb2"),
            _reviewer_entry(3, ok=False, feedback="fb3"),
        ]
    )
    with (
        patch.object(dispatcher, "_git_head_sha", AsyncMock(side_effect=["s0", "s1"])),
        patch.object(dispatcher, "_git_diff_since", AsyncMock(return_value="+x")),
        patch.object(dispatcher, "_run_coder", run_coder),
        patch.object(dispatcher, "_run_reviewer", run_reviewer),
    ):
        result = await dispatch_item(**_KW)

    assert result.ok is False
    assert result.needs_tiebreak is True
    assert run_coder.await_count == 3
    assert run_reviewer.await_count == 3
    assert len(result.transcript) == 6


@pytest.mark.asyncio
async def test_coder_no_diff_re_prompts_then_fails():
    """If coder produces no diff at all, we re-prompt; final no-diff = terminal failure."""
    run_coder = AsyncMock(side_effect=[_coder_entry(1), _coder_entry(2), _coder_entry(3)])
    run_reviewer = AsyncMock()  # never called — diff is always empty
    with (
        patch.object(dispatcher, "_git_head_sha", AsyncMock(return_value="s0")),
        patch.object(dispatcher, "_git_diff_since", AsyncMock(return_value="   \n")),
        patch.object(dispatcher, "_run_coder", run_coder),
        patch.object(dispatcher, "_run_reviewer", run_reviewer),
    ):
        result = await dispatch_item(**_KW)

    assert result.ok is False
    assert result.needs_tiebreak is False  # no point asking architect with no diff
    assert result.failure_reason == "coder_produced_no_diff"
    assert run_coder.await_count == 3
    run_reviewer.assert_not_called()


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
    """Reviewer emits no parseable verdict → treated as reject, feedback explains."""
    run_coder = AsyncMock(side_effect=[_coder_entry(1), _coder_entry(2)])
    run_reviewer = AsyncMock(
        side_effect=[
            _reviewer_entry(1, ok=False, invalid=True),
            _reviewer_entry(2, ok=True),
        ]
    )
    with (
        patch.object(dispatcher, "_git_head_sha", AsyncMock(side_effect=["s0", "s1"])),
        patch.object(dispatcher, "_git_diff_since", AsyncMock(return_value="+x")),
        patch.object(dispatcher, "_run_coder", run_coder),
        patch.object(dispatcher, "_run_reviewer", run_reviewer),
    ):
        result = await dispatch_item(**_KW)

    assert result.ok is True  # second round saved us
    second_call_kwargs = run_coder.await_args_list[1].kwargs
    assert "verdict" in second_call_kwargs["prior_feedback"].lower()
