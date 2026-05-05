"""Tests for ``agent/classifier.py`` — LLM-powered task classifier.

Distinct from ``tests/test_classifier.py``, which tests
``orchestrator/classifier.py`` (the keyword-only classifier).
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
            "classification": "complex",
            "reasoning": "multi-file work",
            "estimated_files": 4,
            "risk": "medium",
        }
    )
    with patch("agent.classifier.get_provider", return_value=_mock_provider(payload)):
        result = await classify_task("Refactor auth", "Touches auth/* and middleware")
    assert result.classification == "complex"
    assert result.estimated_files == 4
    assert result.risk == "medium"
    assert "Classified by keyword heuristics" not in (result.reasoning or "")


@pytest.mark.asyncio
async def test_classifier_handles_fenced_json():
    """Regression: ``agent/classifier.py`` previously hand-rolled brace-locate
    only — a ```json ... ``` wrapped reply silently fell through to keyword
    heuristics. Routing through ``agent.llm.structured.parse_json_response``
    fixes that."""
    payload_obj = {
        "classification": "simple",
        "reasoning": "single-file copy change",
        "estimated_files": 1,
        "risk": "low",
    }
    fenced = "```json\n" + json.dumps(payload_obj) + "\n```"
    with patch("agent.classifier.get_provider", return_value=_mock_provider(fenced)):
        result = await classify_task("Fix typo", "Update README")
    assert result.classification == "simple"
    assert result.risk == "low"
    # The model-derived reasoning should land — not the heuristic fallback string.
    assert result.reasoning == "single-file copy change"


@pytest.mark.asyncio
async def test_classifier_falls_back_on_unparseable_response():
    """Truly unparseable LLM output still falls back to heuristics."""
    with patch("agent.classifier.get_provider", return_value=_mock_provider("¯\\_(ツ)_/¯")):
        result = await classify_task("Fix typo in README", "Just a typo")
    # Heuristic classification — exact value doesn't matter, but the reasoning
    # string is the heuristic-fallback marker.
    assert "heuristics" in (result.reasoning or "").lower()
