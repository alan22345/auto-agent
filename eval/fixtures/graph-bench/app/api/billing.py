"""HTTP-ish handlers for billing."""

from app.services.billing_service import charge_customer


def charge(order) -> dict:
    charge_id = charge_customer(order)
    return {"charge_id": charge_id}
