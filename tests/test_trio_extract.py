"""Tests for ``agent.lifecycle.trio.extract``.

Each extractor is a thin wrapper around ``complete_json`` against
``get_structured_extractor_provider()`` (Bedrock-Haiku). Tests mock the
provider directly so we never touch the network, and verify the shape
each extractor returns under the cases we care about: happy path,
malformed cheap-model output, action-specific required-field validation.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.lifecycle.trio import extract
from agent.lifecycle.trio.extract import (
    extract_checkpoint_output,
    extract_initial_output,
    extract_review_verdict,
    extract_tiebreak_decision,
)
from agent.llm.types import LLMResponse, Message


def _mock_provider(payload: dict | str) -> MagicMock:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(
            message=Message(role="assistant", content=text),
            stop_reason="end_turn",
        )
    )
    return provider


# ---------------------------------------------------------------------------
# run_initial — backlog OR clarification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_initial_returns_backlog():
    payload = {
        "kind": "backlog",
        "items": [
            {"id": "T1", "title": "A", "description": "do a"},
            {"id": "T2", "title": "B", "description": "do b"},
        ],
    }
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_initial_output("architect prose")
    assert result is not None
    assert result["kind"] == "backlog"
    assert [it["id"] for it in result["items"]] == ["T1", "T2"]
    assert all(it["status"] == "pending" for it in result["items"])


@pytest.mark.asyncio
async def test_extract_initial_returns_clarification():
    payload = {"kind": "clarification", "question": "1. Stack? 2. Auth?"}
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_initial_output("architect prose")
    assert result == {"kind": "clarification", "question": "1. Stack? 2. Auth?"}


@pytest.mark.asyncio
async def test_extract_initial_assigns_ids_when_missing():
    payload = {"kind": "backlog", "items": [{"title": "x", "description": "y"}]}
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_initial_output("...")
    assert result["items"][0]["id"] == "T1"


@pytest.mark.asyncio
async def test_extract_initial_returns_none_on_empty_items():
    payload = {"kind": "backlog", "items": []}
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_initial_output("...")
    assert result is None


@pytest.mark.asyncio
async def test_extract_initial_returns_none_when_complete_json_raises():
    """ValueError after exhausted retries → returns None, not propagates."""
    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=LLMResponse(
            message=Message(role="assistant", content="¯\\_(ツ)_/¯"),
            stop_reason="end_turn",
        )
    )
    with patch.object(extract, "get_structured_extractor_provider", return_value=provider):
        result = await extract_initial_output("...")
    assert result is None


# ---------------------------------------------------------------------------
# checkpoint — decision + optional backlog amendment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_checkpoint_done_no_backlog_amendment():
    payload = {
        "decision": {"action": "done", "reason": "all items shipped"},
        "backlog": None,
    }
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_checkpoint_output("checkpoint review")
    assert result["decision"] == {"action": "done", "reason": "all items shipped"}
    assert result["backlog"] is None


@pytest.mark.asyncio
async def test_extract_checkpoint_revise_with_new_backlog():
    payload = {
        "decision": {"action": "revise", "reason": "missed a route"},
        "backlog": [{"id": "T6", "title": "add /healthz", "description": "..."}],
    }
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_checkpoint_output("...")
    assert result["decision"]["action"] == "revise"
    assert result["backlog"][0]["id"] == "T6"


@pytest.mark.asyncio
async def test_extract_checkpoint_awaiting_clarification_requires_question():
    payload = {"decision": {"action": "awaiting_clarification"}, "backlog": None}
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_checkpoint_output("...")
    assert result is None  # missing question


@pytest.mark.asyncio
async def test_extract_checkpoint_rejects_unknown_action():
    payload = {"decision": {"action": "ship it", "reason": "..."}, "backlog": None}
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_checkpoint_output("...")
    assert result is None


# ---------------------------------------------------------------------------
# review verdict — {ok, feedback}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_review_verdict_ok():
    payload = {"ok": True, "feedback": ""}
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_review_verdict("looks good")
    assert result == {"ok": True, "feedback": ""}


@pytest.mark.asyncio
async def test_extract_review_verdict_reject_with_feedback():
    payload = {"ok": False, "feedback": "missing null check on line 42"}
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_review_verdict("...")
    assert result["ok"] is False
    assert "null check" in result["feedback"]


@pytest.mark.asyncio
async def test_extract_review_verdict_returns_none_when_ok_missing():
    payload = {"feedback": "..."}
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_review_verdict("...")
    assert result is None


# ---------------------------------------------------------------------------
# tiebreak — action-conditional required fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_tiebreak_accept_minimal():
    payload = {"action": "accept", "reason": "spec ok"}
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_tiebreak_decision("...")
    assert result == {"action": "accept", "reason": "spec ok"}


@pytest.mark.asyncio
async def test_extract_tiebreak_revise_requires_new_items():
    payload = {"action": "revise_backlog"}  # missing new_items
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_tiebreak_decision("...")
    assert result is None


@pytest.mark.asyncio
async def test_extract_tiebreak_revise_passes_new_items_through():
    payload = {
        "action": "revise_backlog",
        "new_items": [
            {"id": "T1a", "title": "split a", "description": "..."},
            {"id": "T1b", "title": "split b", "description": "..."},
        ],
    }
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_tiebreak_decision("...")
    assert result["action"] == "revise_backlog"
    assert [it["id"] for it in result["new_items"]] == ["T1a", "T1b"]


@pytest.mark.asyncio
async def test_extract_tiebreak_clarify_requires_question():
    payload = {"action": "clarify"}
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_tiebreak_decision("...")
    assert result is None


@pytest.mark.asyncio
async def test_extract_tiebreak_redo_carries_guidance():
    payload = {"action": "redo", "guidance": "fix the null check", "reason": "..."}
    with patch.object(extract, "get_structured_extractor_provider", return_value=_mock_provider(payload)):
        result = await extract_tiebreak_decision("...")
    assert result["action"] == "redo"
    assert result["guidance"] == "fix the null check"
