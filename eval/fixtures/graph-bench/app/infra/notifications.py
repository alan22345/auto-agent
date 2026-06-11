"""Outbound email (fake transport)."""

from app.utils.money import format_cents

SENT: list[dict] = []


def send_email(to: str, subject: str, body: str) -> None:
    SENT.append({"to": to, "subject": subject, "body": body})


def order_confirmation_body(order) -> str:
    lines = [
        f"- {item.product_name} x{item.quantity} @ {format_cents(item.unit_price_cents)}"
        for item in order.items
    ]
    return "Thanks for your order!\n" + "\n".join(lines)
