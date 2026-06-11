"""Payment gateway client (fake)."""


class TransientPaymentError(Exception):
    """Network-ish failure worth retrying."""


class PaymentDeclined(Exception):
    """Hard decline — never retry."""


class PaymentGateway:
    def __init__(self):
        self._charges = []

    def charge(self, customer_id: int, amount_cents: int) -> str:
        """Charge the customer; returns a charge id. May raise either error."""
        charge_id = f"ch_{customer_id}_{len(self._charges)}"
        self._charges.append((customer_id, amount_cents, charge_id))
        return charge_id
