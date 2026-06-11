from app.domain.inventory import restock
from app.domain.models import Customer, LineItem, Order
from app.infra import notifications
from app.services.order_service import place_order


def test_place_order_reserves_prices_and_confirms():
    notifications.SENT.clear()
    restock("Blue Mug", 5)
    order = Order(
        id=1,
        customer=Customer(id=1, email="a@b.c"),
        items=[LineItem("Blue Mug", 500, 2)],
    )

    total = place_order(order)

    assert total == 1000
    assert len(notifications.SENT) == 1
    assert "Blue Mug" in notifications.SENT[0]["body"]
