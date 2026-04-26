"""In-memory store for FoodDash — no external database required.

This is intentionally simple. The point of this project is communication
patterns, not persistence. A dict-based store keeps the focus on what matters.

Thread safety: uses asyncio locks so concurrent async handlers don't corrupt
state. This is NOT production code — it's designed for clarity.
"""

from __future__ import annotations

import asyncio
from shared.models import Customer, Driver, MenuItem, Order, Restaurant


def _seed_restaurant() -> Restaurant:
    """Create a demo restaurant with a few menu items."""
    return Restaurant(
        id="rest_01",
        name="Burger Palace",
        menu=[
            MenuItem(id="item_01", name="Classic Burger", price_cents=999, description="Quarter-pound beef patty"),
            MenuItem(id="item_02", name="Fries", price_cents=399, description="Crispy golden fries"),
            MenuItem(id="item_03", name="Milkshake", price_cents=599, description="Vanilla milkshake"),
        ],
    )


def _seed_drivers() -> list[Driver]:
    return [
        Driver(id="drv_01", name="Alice", latitude=40.7128, longitude=-74.0060),
        Driver(id="drv_02", name="Bob", latitude=40.7580, longitude=-73.9855),
    ]


class DB:
    """Simple in-memory store. Create one instance per server process."""

    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.restaurants: dict[str, Restaurant] = {}
        self.drivers: dict[str, Driver] = {}
        self.customers: dict[str, Customer] = {}
        self._lock = asyncio.Lock()
        self._seed()

    def _seed(self) -> None:
        rest = _seed_restaurant()
        self.restaurants[rest.id] = rest
        for drv in _seed_drivers():
            self.drivers[drv.id] = drv

    async def place_order(self, order: Order) -> Order:
        async with self._lock:
            self.orders[order.id] = order
            return order

    async def get_order(self, order_id: str) -> Order | None:
        return self.orders.get(order_id)

    async def update_order_status(self, order_id: str) -> Order | None:
        """Advance an order to its next status."""
        async with self._lock:
            order = self.orders.get(order_id)
            if order is None:
                return None
            order.advance_status()
            return order

    def get_restaurant(self, restaurant_id: str) -> Restaurant | None:
        return self.restaurants.get(restaurant_id)
