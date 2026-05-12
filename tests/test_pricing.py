"""shared.pricing — static price table + estimate_cost_cents."""

from decimal import Decimal

from shared.pricing import (
    DEFAULT_PRICE_CENTS_PER_MILLION,
    PRICE_PER_MILLION_TOKENS,
    estimate_cost_cents,
)


def test_known_model_costs_match_table() -> None:
    input_cents, output_cents = PRICE_PER_MILLION_TOKENS["claude-sonnet-4-6"]
    expected = Decimal(input_cents) + Decimal(output_cents) / 2
    assert estimate_cost_cents("claude-sonnet-4-6", 1_000_000, 500_000) == expected


def test_unknown_model_uses_default() -> None:
    cost = estimate_cost_cents("nonexistent-future-model-9000", 1_000_000, 0)
    assert cost == Decimal(DEFAULT_PRICE_CENTS_PER_MILLION[0])


def test_zero_tokens_zero_cost() -> None:
    assert estimate_cost_cents("claude-sonnet-4-6", 0, 0) == Decimal(0)
