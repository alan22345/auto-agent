"""Pricing engine — calculates order totals with discounts and tax."""


def calculate_discount(subtotal: float, discount_percent: float) -> float:
    """Return the discount amount for the given subtotal.

    Bug: returns the discounted price instead of the discount amount.
    e.g., for $100 with 10% discount, should return 10.0 but returns 90.0.
    """
    return subtotal * (1 - discount_percent / 100)  # BUG: should be subtotal * (discount_percent / 100)


def calculate_tax(amount: float, tax_rate: float = 0.08) -> float:
    """Return tax for the given amount."""
    return round(amount * tax_rate, 2)


def calculate_total(subtotal: float, discount_percent: float = 0, tax_rate: float = 0.08) -> dict:
    """Calculate the full order total.

    Returns dict with subtotal, discount, tax, and total.
    """
    discount = calculate_discount(subtotal, discount_percent)
    taxable = subtotal - discount
    tax = calculate_tax(taxable, tax_rate)
    total = taxable + tax
    return {
        "subtotal": subtotal,
        "discount": round(discount, 2),
        "tax": tax,
        "total": round(total, 2),
    }
