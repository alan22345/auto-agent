"""Tests for create_repo's LLM-driven repo name generation.

Regression: name generation used to shell out to `claude --print` via
`claude_runner.run_claude_code`. After collapsing `claude_runner/` into
`agent/`, it routes through the same `LLMProvider` seam every other agent
flow uses.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.create_repo import _generate_name_via_claude


@pytest.mark.asyncio
async def test_generate_name_uses_provider_and_sanitizes_output():
    """Provider response is sanitized into a valid GitHub repo slug."""
    mock_response = MagicMock()
    mock_response.message.content = "Cool Project Name!\n"

    mock_provider = AsyncMock()
    mock_provider.complete.return_value = mock_response

    with patch("orchestrator.create_repo.get_provider", return_value=mock_provider):
        name = await _generate_name_via_claude("a tool that does X")

    assert name == "cool-project-name"
    mock_provider.complete.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_name_falls_back_when_provider_raises():
    """If the LLM call fails, fall back to the deterministic slugifier."""
    mock_provider = AsyncMock()
    mock_provider.complete.side_effect = RuntimeError("Bedrock down")

    with patch("orchestrator.create_repo.get_provider", return_value=mock_provider):
        name = await _generate_name_via_claude("build a markdown linter for python")

    # Fallback picks the first 4 words of length > 2
    assert "markdown" in name
    assert name != "new-project"


@pytest.mark.asyncio
async def test_generate_name_falls_back_when_sanitized_to_placeholder():
    """If the model echoes back the placeholder slug, prefer the deterministic fallback."""
    mock_response = MagicMock()
    mock_response.message.content = "new-project"

    mock_provider = AsyncMock()
    mock_provider.complete.return_value = mock_response

    with patch("orchestrator.create_repo.get_provider", return_value=mock_provider):
        name = await _generate_name_via_claude("scrape recipes from blogs")

    # Fallback derives from the description, not the placeholder
    assert "recipes" in name or "scrape" in name
