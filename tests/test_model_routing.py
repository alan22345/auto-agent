"""Tests for agent/llm/__init__.py — model tier routing."""

from agent.llm import MODEL_TIERS


class TestModelTiers:
    def test_fast_tier_exists(self):
        assert "fast" in MODEL_TIERS
        assert "haiku" in MODEL_TIERS["fast"].lower()

    def test_standard_tier_exists(self):
        assert "standard" in MODEL_TIERS
        assert "sonnet" in MODEL_TIERS["standard"].lower()

    def test_capable_tier_exists(self):
        assert "capable" in MODEL_TIERS
        assert "opus" in MODEL_TIERS["capable"].lower()

    def test_tiers_are_valid_model_names(self):
        for tier, model in MODEL_TIERS.items():
            assert "claude" in model, f"Tier '{tier}' model '{model}' doesn't look like a Claude model"

    def test_tier_resolution(self):
        """Verify that tier names resolve to different models."""
        models = set(MODEL_TIERS.values())
        assert len(models) == len(MODEL_TIERS), "Each tier should map to a unique model"

    def test_fast_is_cheapest(self):
        """Haiku should be the fast tier (cheapest)."""
        assert "haiku" in MODEL_TIERS["fast"]

    def test_capable_is_most_expensive(self):
        """Opus should be the capable tier (most expensive)."""
        assert "opus" in MODEL_TIERS["capable"]
