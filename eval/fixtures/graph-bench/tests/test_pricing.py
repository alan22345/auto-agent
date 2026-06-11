from app.domain.models import Customer, LineItem
from app.domain.pricing import compute_total


def _items():
    return [LineItem("Blue Mug", 500, 2), LineItem("Tea Sampler", 1500, 1)]


def test_standard_customer_pays_full_price():
    customer = Customer(id=1, email="a@b.c")
    assert compute_total(_items(), customer) == 2500


def test_silver_customer_gets_five_percent_off():
    customer = Customer(id=2, email="s@b.c", loyalty_tier="silver")
    assert compute_total(_items(), customer) == 2375
