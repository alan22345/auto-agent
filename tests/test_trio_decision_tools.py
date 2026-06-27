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
    SubmitReviewVerdictTool,
)


def _ctx() -> ToolContext:
    return ToolContext(workspace="/tmp/ws")


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
async def test_double_submit_overwrites_last_wins():
    """Tools advise the LLM to call once, but if it calls twice, last
    submission wins; the log tracks the history."""
    sink = DecisionSink()
    await SubmitReviewVerdictTool(sink).execute({"ok": False, "feedback": "a"}, _ctx())
    await SubmitReviewVerdictTool(sink).execute({"ok": True, "feedback": "b"}, _ctx())
    assert sink.review_verdict["ok"] is True
    assert len(sink.log) == 2
