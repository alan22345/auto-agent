"""Tests for build_po_analysis_prompt with the new required `brief` input."""

from __future__ import annotations

from typing import ClassVar

import pytest

from agent.prompts import build_po_analysis_prompt


def _fake_brief(**overrides):
    class FakeBrief:
        product_category: ClassVar[str] = "AI dev tools"
        competitors: ClassVar[list] = [
            {"name": "Cursor", "url": "https://cursor.com", "why_relevant": "AI IDE"},
        ]
        findings: ClassVar[list] = [
            {"theme": "agents",
             "observation": "competitors ship multi-agent",
             "sources": ["https://cursor.com"]},
        ]
        modality_gaps: ClassVar[list] = [
            {"modality": "voice",
             "opportunity": "no voice control today",
             "sources": ["https://cursor.com"]},
        ]
        strategic_themes: ClassVar[list] = [
            {"theme": "AI-native",
             "why_now": "post-GPT-5 momentum",
             "sources": ["https://cursor.com"]},
        ]
        summary: ClassVar[str] = "Market is shifting to multi-modal AI dev tools."
        created_at: ClassVar[None] = None

    b = FakeBrief()
    for k, v in overrides.items():
        setattr(b, k, v)
    return b


def test_po_prompt_requires_brief():
    with pytest.raises(TypeError):
        build_po_analysis_prompt(ux_knowledge="x", recent_suggestions=[], goal=None)  # type: ignore[call-arg]


def test_po_prompt_renders_market_context_section():
    p = build_po_analysis_prompt(
        brief=_fake_brief(), ux_knowledge="x", recent_suggestions=[], goal=None,
    )
    assert "Market context" in p
    assert "Cursor" in p
    assert "cursor.com" in p
    assert "voice" in p.lower()
    assert "AI-native" in p or "ai-native" in p.lower()


def test_po_prompt_requires_evidence_urls_in_output_schema():
    p = build_po_analysis_prompt(
        brief=_fake_brief(), ux_knowledge="x", recent_suggestions=[], goal=None,
    )
    assert "evidence_urls" in p


def test_po_prompt_states_grounding_rule():
    p = build_po_analysis_prompt(
        brief=_fake_brief(), ux_knowledge="x", recent_suggestions=[], goal=None,
    )
    lower = p.lower()
    assert "must be motivated" in lower or "must be grounded" in lower or "drop suggestions" in lower
