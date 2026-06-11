"""Order placement — the main business flow."""

from app.domain.inventory import reserve_items
from app.domain.pricing import compute_total
from app.infra.notifications import order_confirmation_body, send_email
from app.infra.persistence import save_order


def place_order(order) -> int:
    """Reserve stock, price the order, persist, confirm. Returns total cents."""
    reserve_items(order.items, priority=False)
    total_cents = compute_total(order.items, order.customer)
    save_order(order)
    send_email(
        order.customer.email,
        f"Order #{order.id} confirmed",
        order_confirmation_body(order),
    )
    return total_cents
