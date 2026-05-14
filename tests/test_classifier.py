"""Tests for orchestrator/classifier.py — the thin async wrapper that
turns the LLM-driven agent classifier into a (TaskComplexity, result)
tuple for the orchestrator pipeline."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.llm.types import LLMResponse, Message
from orchestrator.classifier import classify_task
from shared.models import TaskComplexity


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


@pytest.mark.asyncio
async def test_ui_only_routes_to_simple():
    payload = {
        "ui_only": True,
        "multi_stage": False,
        "classification": "simple",
        "reasoning": "Just a colour change on the dashboard header.",
    }
    with patch("agent.classifier.get_provider", return_value=_mock_provider(payload)):
        complexity, result = await classify_task(
            "Make the dashboard header green", "Change the brand colour."
        )
    assert complexity == TaskComplexity.SIMPLE
    assert result.classification == "simple"


@pytest.mark.asyncio
async def test_multi_stage_routes_to_complex_large():
    payload = {
        "ui_only": False,
        "multi_stage": True,
        "classification": "complex_large",
        "reasoning": "Spans auth, billing, and admin — needs phased rollout.",
    }
    with patch("agent.classifier.get_provider", return_value=_mock_provider(payload)):
        complexity, result = await classify_task(
            "Add multi-tenant billing",
            "Subscription tiers, Stripe webhooks, admin panel, and a backfill.",
        )
    assert complexity == TaskComplexity.COMPLEX_LARGE
    assert result.classification == "complex_large"


@pytest.mark.asyncio
async def test_in_between_routes_to_complex():
    payload = {
        "ui_only": False,
        "multi_stage": False,
        "classification": "complex",
        "reasoning": "Logic plus a couple of files, but lands in one pass.",
    }
    with patch("agent.classifier.get_provider", return_value=_mock_provider(payload)):
        complexity, result = await classify_task(
            "Add a /health endpoint",
            "New FastAPI route plus a small DB check.",
        )
    assert complexity == TaskComplexity.COMPLEX
    assert result.classification == "complex"


@pytest.mark.asyncio
async def test_falls_back_to_complex_on_llm_failure():
    provider = MagicMock()
    provider.complete = AsyncMock(side_effect=RuntimeError("Bedrock 503"))
    with patch("agent.classifier.get_provider", return_value=provider):
        complexity, result = await classify_task("Anything", "Anything")
    assert complexity == TaskComplexity.COMPLEX
    assert "default" in result.reasoning.lower() or "unavailable" in result.reasoning.lower()


@pytest.mark.asyncio
async def test_unknown_label_recovers_from_binary_answers():
    payload = {
        "ui_only": False,
        "multi_stage": True,
        "classification": "humongous",
        "reasoning": "Model invented a label.",
    }
    with patch("agent.classifier.get_provider", return_value=_mock_provider(payload)):
        complexity, _ = await classify_task("Big work", "Lots of phases.")
    assert complexity == TaskComplexity.COMPLEX_LARGE
