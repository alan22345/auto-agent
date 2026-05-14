"""Tests for ``agent/classifier.py`` — LLM-driven task classifier.

Distinct from ``tests/test_classifier.py``, which tests the orchestrator
wrapper that maps the Pydantic result to a ``TaskComplexity`` enum.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.classifier import classify_task
from agent.llm.types import LLMResponse, Message


def _mock_provider(text_response: str) -> MagicMock:
    provider = MagicMock()
    response = LLMResponse(
        message=Message(role="assistant", content=text_response),
        stop_reason="end_turn",
    )
    provider.complete = AsyncMock(return_value=response)
    return provider


@pytest.mark.asyncio
async def test_classifier_handles_bare_json():
    payload = json.dumps(
        {
            "ui_only": False,
            "multi_stage": False,
            "classification": "complex",
            "reasoning": "Multi-file but lands in one pass.",
        }
    )
    with patch("agent.classifier.get_provider", return_value=_mock_provider(payload)):
        result = await classify_task("Refactor auth", "Touches auth/* and middleware")
    assert result.classification == "complex"
    assert result.reasoning == "Multi-file but lands in one pass."


@pytest.mark.asyncio
async def test_classifier_handles_fenced_json():
    """Regression: fenced ```json ... ``` replies must parse via
    ``agent.llm.structured.parse_json_response``."""
    payload_obj = {
        "ui_only": True,
        "multi_stage": False,
        "classification": "simple",
        "reasoning": "Just a copy tweak in the header.",
    }
    fenced = "```json\n" + json.dumps(payload_obj) + "\n```"
    with patch("agent.classifier.get_provider", return_value=_mock_provider(fenced)):
        result = await classify_task("Fix header copy", "Update wording")
    assert result.classification == "simple"
    assert result.reasoning == "Just a copy tweak in the header."


@pytest.mark.asyncio
async def test_classifier_emits_complex_large_for_multi_stage_work():
    payload = json.dumps(
        {
            "ui_only": False,
            "multi_stage": True,
            "classification": "complex_large",
            "reasoning": "Spans backend + frontend + migration.",
        }
    )
    with patch("agent.classifier.get_provider", return_value=_mock_provider(payload)):
        result = await classify_task(
            "Multi-tenant billing",
            "Tiers, webhooks, admin panel, backfill.",
        )
    assert result.classification == "complex_large"


@pytest.mark.asyncio
async def test_classifier_defaults_to_complex_on_llm_failure():
    """No keyword fallback any more — failures default to ``complex``."""
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=RuntimeError("Bedrock 503"))
    with patch("agent.classifier.get_provider", return_value=provider):
        result = await classify_task("Anything", "Anything")
    assert result.classification == "complex"
    assert "default" in result.reasoning.lower() or "unavailable" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_classifier_recovers_from_unknown_label():
    """If the model invents a label, we re-derive from the two binary
    answers rather than crashing."""
    payload = json.dumps(
        {
            "ui_only": True,
            "multi_stage": False,
            "classification": "trivial",  # not in the valid set
            "reasoning": "n/a",
        }
    )
    with patch("agent.classifier.get_provider", return_value=_mock_provider(payload)):
        result = await classify_task("Tweak", "Style")
    assert result.classification == "simple"


@pytest.mark.asyncio
async def test_classifier_prompt_names_the_three_buckets():
    """The prompt must mention each bucket name so the model knows the
    full label set it can emit."""
    provider = _mock_provider(
        json.dumps(
            {
                "ui_only": False,
                "multi_stage": False,
                "classification": "complex",
                "reasoning": ".",
            }
        )
    )
    with patch("agent.classifier.get_provider", return_value=provider):
        await classify_task("Anything", "Anything")
    sent_prompt = provider.complete.call_args.kwargs["messages"][0].content
    for bucket in ("simple", "complex", "complex_large"):
        assert bucket in sent_prompt, f"prompt missing bucket label {bucket!r}"
    assert "ui_only" in sent_prompt
    assert "multi_stage" in sent_prompt
