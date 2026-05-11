"""Static price table for LLM calls. Cents per million tokens.

Numbers are estimates only — they go into `usage_events.cost_cents` for
operational visibility, not into billing. Phase 5 owns billing accuracy.

Source of truth: anthropic.com/pricing as of 2026-05-11. Update this table
when pricing changes. Treat additions as additive only — never delete a
key for a deprecated model, because old `usage_events` rows still reference
it indirectly via `model` text.
"""

from __future__ import annotations

from decimal import Decimal

# (input_cents_per_million, output_cents_per_million)
PRICE_PER_MILLION_TOKENS: dict[str, tuple[int, int]] = {
    "claude-sonnet-4-6": (300, 1500),       # $3 / $15
    "claude-opus-4-6": (1500, 7500),        # $15 / $75
    "claude-haiku-4-5": (80, 400),          # $0.80 / $4
    "claude-sonnet-4-20250514": (300, 1500),
    "claude-opus-4-20250514": (1500, 7500),
}

# Fallback for unknown / passthrough providers. Picked to slightly over-estimate
# so quota gates fail safe rather than letting cost-unknown traffic through.
DEFAULT_PRICE_CENTS_PER_MILLION: tuple[int, int] = (500, 2000)


def estimate_cost_cents(
    model: str, input_tokens: int, output_tokens: int
) -> Decimal:
    """Return estimated cost in cents (Decimal — supports fractions)."""
    in_cents_per_m, out_cents_per_m = PRICE_PER_MILLION_TOKENS.get(
        model, DEFAULT_PRICE_CENTS_PER_MILLION
    )
    cost = (
        Decimal(input_tokens) * Decimal(in_cents_per_m) / Decimal(1_000_000)
        + Decimal(output_tokens) * Decimal(out_cents_per_m) / Decimal(1_000_000)
    )
    return cost
