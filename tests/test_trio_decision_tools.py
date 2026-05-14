"""Tests for ``agent.tools.trio_decision``.

The tools are stateless beyond a caller-supplied DecisionSink — these
tests exercise each tool's execute() against a real sink and assert
the sink ends up shaped correctly.
"""

from __future__ import annotations

import pytest

from agent.tools.base import ToolContext
from agent.tools.trio_decision import (
    DecisionSink,
    SubmitBacklogTool,
    SubmitCheckpointDecisionTool,
    SubmitClarificationTool,
    SubmitReviewVerdictTool,
    SubmitTiebreakTool,
)


def _ctx() -> ToolContext:
    return ToolContext(workspace="/tmp/ws")


@pytest.mark.asyncio
async def test_submit_backlog_normalises_and_records():
    sink = DecisionSink()
    tool = SubmitBacklogTool(sink)
    result = await tool.execute(
        {"items": [
            {"id": "T1", "title": "first", "description": "do a"},
            {"id": "T2", "title": "second", "description": "do b"},
        ]},
        _ctx(),
    )
    assert not result.is_error
    assert sink.backlog is not None
    assert [it["id"] for it in sink.backlog] == ["T1", "T2"]
    assert all(it["status"] == "pending" for it in sink.backlog)


@pytest.mark.asyncio
async def test_submit_backlog_rejects_empty_items():
    sink = DecisionSink()
    result = await SubmitBacklogTool(sink).execute({"items": []}, _ctx())
    assert result.is_error
    assert sink.backlog is None


@pytest.mark.asyncio
async def test_submit_backlog_assigns_ids_when_missing():
    sink = DecisionSink()
    result = await SubmitBacklogTool(sink).execute(
        {"items": [
            {"title": "no-id", "description": "x"},
        ]},
        _ctx(),
    )
    assert not result.is_error
    assert sink.backlog[0]["id"] == "T1"


@pytest.mark.asyncio
async def test_submit_clarification_records_question():
    sink = DecisionSink()
    result = await SubmitClarificationTool(sink).execute(
        {"question": "1. Stack? 2. Auth?"}, _ctx(),
    )
    assert not result.is_error
    assert sink.clarification == "1. Stack? 2. Auth?"


@pytest.mark.asyncio
async def test_submit_clarification_rejects_empty():
    sink = DecisionSink()
    result = await SubmitClarificationTool(sink).execute({"question": "  "}, _ctx())
    assert result.is_error
    assert sink.clarification is None


@pytest.mark.asyncio
async def test_submit_checkpoint_records_valid_action():
    sink = DecisionSink()
    result = await SubmitCheckpointDecisionTool(sink).execute(
        {"action": "done", "reason": "all items merged"}, _ctx(),
    )
    assert not result.is_error
    assert sink.checkpoint == {"action": "done", "reason": "all items merged"}


@pytest.mark.asyncio
async def test_submit_checkpoint_rejects_unknown_action():
    sink = DecisionSink()
    result = await SubmitCheckpointDecisionTool(sink).execute(
        {"action": "ship it"}, _ctx(),
    )
    assert result.is_error
    assert sink.checkpoint is None


@pytest.mark.asyncio
async def test_submit_checkpoint_passes_question_for_clarification():
    sink = DecisionSink()
    await SubmitCheckpointDecisionTool(sink).execute(
        {"action": "awaiting_clarification", "question": "Which db?"}, _ctx(),
    )
    assert sink.checkpoint["question"] == "Which db?"


@pytest.mark.asyncio
async def test_submit_review_verdict_records_ok():
    sink = DecisionSink()
    result = await SubmitReviewVerdictTool(sink).execute(
        {"ok": True, "feedback": ""}, _ctx(),
    )
    assert not result.is_error
    assert sink.review_verdict == {"ok": True, "feedback": ""}


@pytest.mark.asyncio
async def test_submit_review_verdict_records_reject_with_feedback():
    sink = DecisionSink()
    await SubmitReviewVerdictTool(sink).execute(
        {"ok": False, "feedback": "missing null check on line 42"}, _ctx(),
    )
    assert sink.review_verdict["ok"] is False
    assert "null check" in sink.review_verdict["feedback"]


@pytest.mark.asyncio
async def test_submit_tiebreak_accept_records_decision():
    sink = DecisionSink()
    result = await SubmitTiebreakTool(sink).execute(
        {"action": "accept", "reason": "spec ok"}, _ctx(),
    )
    assert not result.is_error
    assert sink.tiebreak["action"] == "accept"
    assert sink.tiebreak["reason"] == "spec ok"


@pytest.mark.asyncio
async def test_submit_tiebreak_revise_backlog_normalises_new_items():
    sink = DecisionSink()
    await SubmitTiebreakTool(sink).execute(
        {
            "action": "revise_backlog",
            "new_items": [
                {"id": "T1a", "title": "split a", "description": "..."},
                {"id": "T1b", "title": "split b", "description": "..."},
            ],
        },
        _ctx(),
    )
    assert sink.tiebreak["action"] == "revise_backlog"
    assert len(sink.tiebreak["new_items"]) == 2


@pytest.mark.asyncio
async def test_submit_tiebreak_rejects_unknown_action():
    sink = DecisionSink()
    result = await SubmitTiebreakTool(sink).execute({"action": "shrug"}, _ctx())
    assert result.is_error
    assert sink.tiebreak is None


@pytest.mark.asyncio
async def test_double_submit_overwrites_last_wins():
    """Tools advise the LLM to call once, but if it calls twice, last
    submission wins; the log tracks the history."""
    sink = DecisionSink()
    await SubmitCheckpointDecisionTool(sink).execute({"action": "continue"}, _ctx())
    await SubmitCheckpointDecisionTool(sink).execute({"action": "done"}, _ctx())
    assert sink.checkpoint["action"] == "done"
    assert len(sink.log) == 2
