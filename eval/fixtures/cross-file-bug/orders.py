"""Order processing — uses pricing engine to build order summaries."""

from pricing import calculate_total


class Order:
    def __init__(self, items: list[dict], discount_percent: float = 0):
        self.items = items
        self.discount_percent = discount_percent

    def get_subtotal(self) -> float:
        return sum(item["price"] * item["qty"] for item in self.items)

    def get_summary(self) -> dict:
        subtotal = self.get_subtotal()
        totals = calculate_total(subtotal, self.discount_percent)
        return {
            "items": len(self.items),
            "subtotal": totals["subtotal"],
            "discount": totals["discount"],
            "tax": totals["tax"],
            "total": totals["total"],
        }
