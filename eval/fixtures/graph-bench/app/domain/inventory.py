"""In-memory inventory ledger."""

_STOCK: dict[str, int] = {}


def stock_level(product_name: str) -> int:
    return _STOCK.get(product_name, 0)


def restock(product_name: str, quantity: int) -> None:
    _STOCK[product_name] = stock_level(product_name) + quantity


def reserve_items(items, priority=False):
    """Reserve stock for each line item; raises on shortage."""
    for item in items:
        available = stock_level(item.product_name)
        if available < item.quantity:
            raise ValueError(f"insufficient stock for {item.product_name}")
    for item in items:
        _STOCK[item.product_name] = stock_level(item.product_name) - item.quantity


def release_items(items) -> None:
    for item in items:
        restock(item.product_name, item.quantity)
