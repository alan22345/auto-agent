"""HTTP-ish handlers for orders (framework-free for the exercise)."""

from app.domain.inventory import reserve_items
from app.domain.pricing import compute_total
from app.infra.persistence import load_order


def create_order(order) -> dict:
    reserve_items(order.items, priority=True)
    total = compute_total(order.items, order.customer)
    return {"order_id": order.id, "total_cents": total}


def get_order(order_id: int) -> dict:
    order = load_order(order_id)
    if order is None:
        return {"error": "not found"}
    total = compute_total(order.items, order.customer)
    return {"order_id": order.id, "total_cents": total}
