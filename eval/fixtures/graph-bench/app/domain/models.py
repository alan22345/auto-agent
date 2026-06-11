"""Domain models for the order service."""

from dataclasses import dataclass, field


@dataclass
class LineItem:
    product_name: str
    unit_price_cents: int
    quantity: int


@dataclass
class Customer:
    id: int
    email: str
    loyalty_tier: str = "standard"  # standard | silver | gold


@dataclass
class Order:
    id: int
    customer: Customer
    items: list[LineItem] = field(default_factory=list)
