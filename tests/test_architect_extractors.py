"""Tests for the JSON extractors in agent/lifecycle/trio/architect.py."""
from __future__ import annotations

from agent.lifecycle.trio.architect import (
    _extract_backlog,
    _extract_clarification,
)


def test_extract_clarification_returns_question_string():
    text = (
        "Some reasoning.\n\n"
        '```json\n'
        '{"decision": {"action": "awaiting_clarification", "question": "Q?"}}\n'
        '```\n'
    )
    assert _extract_clarification(text) == "Q?"


def test_extract_clarification_returns_none_when_no_block():
    assert _extract_clarification("no json here") is None


def test_extract_clarification_returns_none_when_action_is_not_awaiting():
    text = '```json\n{"decision": {"action": "done", "reason": "shipped"}}\n```'
    assert _extract_clarification(text) is None


def test_extract_clarification_returns_none_when_question_missing():
    text = '```json\n{"decision": {"action": "awaiting_clarification"}}\n```'
    assert _extract_clarification(text) is None


def test_extract_clarification_picks_last_valid_block():
    """Two JSON blocks — clarification wins if it's last and valid."""
    text = (
        '```json\n{"backlog": [{"id": "1", "title": "x", "description": "y"}]}\n```\n'
        'But actually wait,\n'
        '```json\n{"decision": {"action": "awaiting_clarification", "question": "Q?"}}\n```'
    )
    assert _extract_clarification(text) == "Q?"


def test_backlog_takes_precedence_when_clarification_absent():
    """Existing behaviour: backlog still extracts when present alone."""
    text = '```json\n{"backlog": [{"id": "1", "title": "x", "description": "y"}]}\n```'
    backlog = _extract_backlog(text)
    assert backlog is not None
    assert len(backlog) == 1
