"""Tests for the market_research prompt builder."""

from agent.prompts import build_market_research_prompt


def test_market_research_prompt_renders():
    p = build_market_research_prompt(repo_name="acme-app")
    assert "acme-app" in p
    assert "STRICT JSON" in p  # output discipline


def test_market_research_prompt_excludes_package_json():
    """Regression: package.json is too long, we explicitly excluded it."""
    p = build_market_research_prompt(repo_name="acme-app")
    assert "package.json" not in p


def test_market_research_prompt_mentions_three_lenses():
    p = build_market_research_prompt(repo_name="acme-app")
    lower = p.lower()
    assert "competitor" in lower
    assert "modality" in lower or "voice" in lower
    assert "strategic" in lower or "why now" in lower


def test_market_research_prompt_requires_citations():
    p = build_market_research_prompt(repo_name="acme-app")
    assert "cite" in p.lower() or "source" in p.lower()
