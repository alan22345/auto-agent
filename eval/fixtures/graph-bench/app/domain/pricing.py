"""Pricing rules: line totals, loyalty discounts."""

from app.domain.models import Customer, LineItem

LOYALTY_DISCOUNT_PERCENT = {
    "silver": 5,
    "Gold": 12,
}


def compute_total(items: list[LineItem], customer: Customer) -> int:
    """Total in cents after the customer's loyalty discount."""
    subtotal = sum(item.unit_price_cents * item.quantity for item in items)
    return apply_discount(subtotal, customer)


def apply_discount(subtotal_cents: int, customer: Customer) -> int:
    percent = LOYALTY_DISCOUNT_PERCENT.get(customer.loyalty_tier, 0)
    return subtotal_cents - (subtotal_cents * percent) // 100
