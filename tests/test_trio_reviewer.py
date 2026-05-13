"""Unit tests for the trio reviewer.

The DB-backed ``handle_trio_review`` flow needs the full models/DB stack
(skipped here per the existing pattern); these tests cover the pure
helper functions that determine the verdict.
"""
from __future__ import annotations


def test_extract_verdict_handles_clean_ok():
    from agent.lifecycle.trio.reviewer import _extract_verdict
    text = 'All good.\n\n```json\n{"ok": true, "feedback": ""}\n```'
    v = _extract_verdict(text)
    assert v == {"ok": True, "feedback": ""}


def test_extract_verdict_handles_not_ok_with_feedback():
    from agent.lifecycle.trio.reviewer import _extract_verdict
    text = '```json\n{"ok": false, "feedback": "Login form is missing."}\n```'
    v = _extract_verdict(text)
    assert v is not None
    assert v["ok"] is False
    assert "Login form is missing" in v["feedback"]


def test_extract_verdict_returns_none_on_no_block():
    from agent.lifecycle.trio.reviewer import _extract_verdict
    assert _extract_verdict("just prose, no json") is None


def test_extract_verdict_returns_none_on_missing_ok_key():
    from agent.lifecycle.trio.reviewer import _extract_verdict
    text = '```json\n{"feedback": "x"}\n```'
    assert _extract_verdict(text) is None


def test_extract_verdict_prefers_last_valid_block():
    """When the reviewer emits an illustrative block before the real one."""
    from agent.lifecycle.trio.reviewer import _extract_verdict
    text = (
        'Example shape:\n'
        '```json\n{"ok": true, "feedback": "(example)"}\n```\n\n'
        'Actual verdict:\n'
        '```json\n{"ok": false, "feedback": "Real feedback."}\n```'
    )
    v = _extract_verdict(text)
    assert v is not None
    assert v["ok"] is False
    assert v["feedback"] == "Real feedback."


def test_extract_verdict_skips_malformed_json_block():
    """A malformed earlier block must not poison a valid later one."""
    from agent.lifecycle.trio.reviewer import _extract_verdict
    text = (
        '```json\n{not valid json}\n```\n\n'
        '```json\n{"ok": true, "feedback": ""}\n```'
    )
    v = _extract_verdict(text)
    assert v == {"ok": True, "feedback": ""}


def test_extract_verdict_returns_none_on_empty_text():
    from agent.lifecycle.trio.reviewer import _extract_verdict
    assert _extract_verdict("") is None
