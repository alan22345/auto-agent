"""Tests for order processing — these fail due to a bug in the pricing module."""

from orders import Order


def test_order_no_discount():
    """Order with no discount should just add tax."""
    order = Order(
        items=[{"name": "Widget", "price": 100.0, "qty": 1}],
        discount_percent=0,
    )
    summary = order.get_summary()
    assert summary["subtotal"] == 100.0
    assert summary["discount"] == 0.0
    assert summary["tax"] == 8.0
    assert summary["total"] == 108.0


def test_order_with_discount():
    """10% discount on $200 should discount $20, tax the remaining $180."""
    order = Order(
        items=[
            {"name": "Widget", "price": 50.0, "qty": 2},
            {"name": "Gadget", "price": 100.0, "qty": 1},
        ],
        discount_percent=10,
    )
    summary = order.get_summary()
    assert summary["subtotal"] == 200.0
    assert summary["discount"] == 20.0   # FAILS: calculate_discount returns 180 instead of 20
    assert summary["tax"] == 14.4        # FAILS: tax is on wrong amount
    assert summary["total"] == 194.4     # FAILS: total is wrong


def test_order_with_large_discount():
    """50% discount on $80 should discount $40."""
    order = Order(
        items=[{"name": "Sale Item", "price": 20.0, "qty": 4}],
        discount_percent=50,
    )
    summary = order.get_summary()
    assert summary["subtotal"] == 80.0
    assert summary["discount"] == 40.0   # FAILS
    assert summary["tax"] == 3.2
    assert summary["total"] == 43.2      # FAILS


def test_multiple_items():
    """Multiple items with no discount."""
    order = Order(
        items=[
            {"name": "A", "price": 10.0, "qty": 3},
            {"name": "B", "price": 25.0, "qty": 2},
        ],
        discount_percent=0,
    )
    summary = order.get_summary()
    assert summary["items"] == 2
    assert summary["subtotal"] == 80.0
    assert summary["total"] == 86.4
