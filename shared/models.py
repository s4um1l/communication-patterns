"""FoodDash domain models — shared across all chapters.

These models represent our food delivery platform. As the system evolves
through chapters, we use the same domain to show how different communication
patterns solve different problems for the same business.
"""

from __future__ import annotations

import enum
import time
import uuid
from pydantic import BaseModel, Field


class OrderStatus(str, enum.Enum):
    """An order's lifecycle — each transition is an event other services care about."""

    PLACED = "placed"  # Customer submitted the order
    CONFIRMED = "confirmed"  # Restaurant accepted it
    PREPARING = "preparing"  # Kitchen is making the food
    READY = "ready"  # Food is ready for pickup
    PICKED_UP = "picked_up"  # Driver picked it up
    EN_ROUTE = "en_route"  # Driver is on the way
    DELIVERED = "delivered"  # Customer received the food
    CANCELLED = "cancelled"  # Order was cancelled


# The natural progression — used to validate transitions
ORDER_FLOW = [
    OrderStatus.PLACED,
    OrderStatus.CONFIRMED,
    OrderStatus.PREPARING,
    OrderStatus.READY,
    OrderStatus.PICKED_UP,
    OrderStatus.EN_ROUTE,
    OrderStatus.DELIVERED,
]


class MenuItem(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str
    price_cents: int  # Store money as integers to avoid float issues
    description: str = ""


class OrderItem(BaseModel):
    menu_item: MenuItem
    quantity: int = 1

    @property
    def subtotal_cents(self) -> int:
        return self.menu_item.price_cents * self.quantity


class Customer(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str
    address: str = ""


class Driver(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str
    latitude: float = 0.0
    longitude: float = 0.0
    available: bool = True


class Restaurant(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str
    menu: list[MenuItem] = []


class Order(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    customer: Customer
    restaurant_id: str
    items: list[OrderItem]
    status: OrderStatus = OrderStatus.PLACED
    driver_id: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @property
    def total_cents(self) -> int:
        return sum(item.subtotal_cents for item in self.items)

    def advance_status(self) -> OrderStatus:
        """Move to the next status in the natural flow. Returns the new status."""
        try:
            idx = ORDER_FLOW.index(self.status)
        except ValueError:
            raise ValueError(f"Cannot advance from {self.status}")
        if idx + 1 >= len(ORDER_FLOW):
            raise ValueError(f"Order already at final status: {self.status}")
        self.status = ORDER_FLOW[idx + 1]
        self.updated_at = time.time()
        return self.status
