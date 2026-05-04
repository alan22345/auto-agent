"""Spec for ``agent.llm.structured`` — the single owner of LLM-output → dict.

Two functions are tested here:

- ``parse_json_response(text) -> dict | None`` — pure: strip fences, locate
  braces, decode. Returns ``None`` (never raises) so callers pick the
  fallback policy at the call site.
- ``complete_json(provider, ...)`` — one-shot LLM call + parse + bounded
  retry with a "your last response wasn't valid JSON" nudge. Raises
  ``ValueError`` after exhausting retries.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.llm.structured import complete_json, parse_json_response
from agent.llm.types import LLMResponse, Message

# ---------------------------------------------------------------------------
# parse_json_response
# ---------------------------------------------------------------------------


def test_parse_bare_json():
    assert parse_json_response('{"a": 1}') == {"a": 1}


def test_parse_with_surrounding_whitespace():
    assert parse_json_response('   \n{"a": 1}\n  ') == {"a": 1}


def test_parse_fenced_no_language_tag():
    raw = '```\n{"a": 1}\n```'
    assert parse_json_response(raw) == {"a": 1}


def test_parse_fenced_with_json_tag():
    """The case ``agent/classifier.py`` previously missed."""
    raw = '```json\n{"classification": "simple"}\n```'
    assert parse_json_response(raw) == {"classification": "simple"}


def test_parse_prose_preamble_then_json():
    """First ``{`` / last ``}`` brace location handles a prose preamble."""
    raw = 'Here is the JSON you asked for:\n{"a": 1, "b": [2, 3]}'
    assert parse_json_response(raw) == {"a": 1, "b": [2, 3]}


def test_parse_garbage_returns_none():
    assert parse_json_response("not json at all") is None


def test_parse_empty_returns_none():
    assert parse_json_response("") is None
    assert parse_json_response("   \n  ") is None


def test_parse_top_level_list_returns_none():
    """Design choice: callers all want objects. A top-level list is rejected
    rather than returned, so no caller silently gets a wrong-shaped value."""
    assert parse_json_response("[1, 2, 3]") is None


def test_parse_invalid_json_returns_none():
    """First/last brace can be located but slice doesn't decode."""
    assert parse_json_response("prefix {not valid json} suffix") is None


def test_parse_fenced_json_with_inner_braces():
    raw = '```json\n{"nested": {"k": "v"}, "arr": [{"x": 1}]}\n```'
    parsed = parse_json_response(raw)
    assert parsed == {"nested": {"k": "v"}, "arr": [{"x": 1}]}


# ---------------------------------------------------------------------------
# complete_json
# ---------------------------------------------------------------------------


def _mock_provider_sequence(*texts: str) -> MagicMock:
    provider = MagicMock()
    responses = [
        LLMResponse(message=Message(role="assistant", content=t), stop_reason="end_turn")
        for t in texts
    ]
    provider.complete = AsyncMock(side_effect=responses)
    return provider


@pytest.mark.asyncio
async def test_complete_json_first_attempt_succeeds():
    provider = _mock_provider_sequence(json.dumps({"ok": True}))
    result = await complete_json(
        provider,
        messages=[Message(role="user", content="hi")],
        system="be a JSON robot",
    )
    assert result == {"ok": True}
    assert provider.complete.await_count == 1


@pytest.mark.asyncio
async def test_complete_json_retries_after_bad_response():
    provider = _mock_provider_sequence(
        "not json at all",
        json.dumps({"ok": True}),
    )
    result = await complete_json(
        provider,
        messages=[Message(role="user", content="hi")],
        system="be a JSON robot",
    )
    assert result == {"ok": True}
    assert provider.complete.await_count == 2


@pytest.mark.asyncio
async def test_complete_json_raises_after_exhausting_retries():
    provider = _mock_provider_sequence("garbage 1", "garbage 2")
    with pytest.raises(ValueError, match="could not parse"):
        await complete_json(
            provider,
            messages=[Message(role="user", content="hi")],
            system="be a JSON robot",
            retries=2,
        )
    assert provider.complete.await_count == 2


@pytest.mark.asyncio
async def test_complete_json_retry_includes_nudge_in_system():
    provider = _mock_provider_sequence("garbage", json.dumps({"ok": True}))
    await complete_json(
        provider,
        messages=[Message(role="user", content="hi")],
        system="original system",
    )
    assert provider.complete.await_count == 2
    first_system = provider.complete.await_args_list[0].kwargs["system"]
    second_system = provider.complete.await_args_list[1].kwargs["system"]
    assert first_system == "original system"
    assert "original system" in second_system
    assert "valid JSON" in second_system


@pytest.mark.asyncio
async def test_complete_json_passes_through_max_tokens_and_temperature():
    provider = _mock_provider_sequence(json.dumps({"ok": True}))
    await complete_json(
        provider,
        messages=[Message(role="user", content="hi")],
        system="s",
        max_tokens=512,
        temperature=0.2,
    )
    kwargs = provider.complete.await_args_list[0].kwargs
    assert kwargs["max_tokens"] == 512
    assert kwargs["temperature"] == 0.2


@pytest.mark.asyncio
async def test_complete_json_schema_hint_appended_to_first_system():
    provider = _mock_provider_sequence(json.dumps({"ok": True}))
    await complete_json(
        provider,
        messages=[Message(role="user", content="hi")],
        system="be a JSON robot",
        schema_hint='{"facts": [{"entity": "..."}]}',
    )
    first_system = provider.complete.await_args_list[0].kwargs["system"]
    assert "schema" in first_system.lower() or '"facts"' in first_system
