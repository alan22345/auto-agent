"""In-memory order store with a flaky write path."""

from app.utils.retry import retry_with_backoff

_ORDERS: dict[int, object] = {}


def save_order(order) -> None:
    """Persist the order; the (fake) store is flaky, so writes retry."""

    def _write():
        _ORDERS[order.id] = order

    retry_with_backoff(_write, attempts=3)


def load_order(order_id: int):
    return _ORDERS.get(order_id)
