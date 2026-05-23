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
    """Provider response is sanitized into a valid GitHub repo slug.

    Also asserts the fast tier (Haiku) is requested — naming is the
    canonical fast-tier use case per agent/llm/__init__.py::MODEL_TIERS.
    """
    mock_response = MagicMock()
    mock_response.message.content = "Cool Project Name!\n"

    mock_provider = AsyncMock()
    mock_provider.complete.return_value = mock_response

    with patch(
        "orchestrator.create_repo.get_provider", return_value=mock_provider
    ) as mock_get_provider:
        name = await _generate_name_via_claude("a tool that does X")

    assert name == "cool-project-name"
    mock_provider.complete.assert_awaited_once()
    mock_get_provider.assert_called_once_with(model_override="fast")


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


# ---------------------------------------------------------------------------
# Title extraction — regression for the "section of a series" mis-scope bug.
# A user submitted a long brief whose first line was ``## 1. What this
# service is, in one paragraph``. The old code took that line verbatim as the
# task title, and the intent-grill agent inferred "first scaffold of a
# series" → produced foundation-only domains.
# ---------------------------------------------------------------------------


def test_first_prose_line_skips_markdown_headers():
    from orchestrator.create_repo import _first_prose_line

    description = (
        "## 1. What this service is, in one paragraph\n\n"
        "A GTM outbound automation service that owns the strategy ends ...\n"
    )
    assert _first_prose_line(description).startswith("A GTM outbound")


def test_first_prose_line_skips_blockquotes_and_numbered_markers():
    from orchestrator.create_repo import _first_prose_line

    description = (
        "# Title\n"
        "> blockquote\n"
        "1) numbered marker\n"
        "Real first sentence.\n"
    )
    assert _first_prose_line(description) == "Real first sentence."


def test_first_prose_line_returns_empty_when_only_structural_lines():
    from orchestrator.create_repo import _first_prose_line

    description = "## header\n\n# another header\n"
    assert _first_prose_line(description) == ""
