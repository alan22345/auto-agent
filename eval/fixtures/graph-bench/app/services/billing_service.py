"""Charging customers for placed orders."""

from app.domain.pricing import compute_total
from app.infra.notifications import send_email
from app.infra.payments import PaymentGateway

_gateway = PaymentGateway()


def charge_customer(order) -> str:
    """Charge the order total; returns the gateway charge id."""
    amount = compute_total(order.items, order.customer)
    charge_id = _gateway.charge(order.customer.id, amount)
    send_email(
        order.customer.email,
        f"Receipt for order #{order.id}",
        f"Charged {amount} cents (charge {charge_id}).",
    )
    return charge_id


def invoice_total(order) -> int:
    """Total shown on the invoice — must match what was charged."""
    return compute_total(order.items, order.customer)
