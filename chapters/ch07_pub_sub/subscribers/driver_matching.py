"""Chapter 07 -- Driver Matching Subscriber

Finds the nearest available driver and assigns them to the order.

This subscriber demonstrates TWO important pub/sub concepts:

1. **Topic filtering**: It subscribes to "order.confirmed" (not "order.placed").
   There is no point matching a driver before the restaurant confirms the order.
   The kitchen might reject it (out of ingredients). Pub/sub lets each
   subscriber pick the RIGHT event to react to.

2. **Multiple subscriptions**: It also subscribes to "order.cancelled" to
   RELEASE a matched driver. A single service can subscribe to multiple
   topics. Each subscription is independent.

Subscribes to: order.confirmed, order.cancelled
Processing time: ~300ms (query driver locations, calculate distances, assign)
"""

from __future__ import annotations

import asyncio
import random


class DriverMatchingSubscriber:
    """Matches available drivers to confirmed orders."""

    def __init__(self) -> None:
        # Track which orders have assigned drivers (for idempotency)
        self._assignments: dict[str, str] = {}  # order_id -> driver_name
        self.matches_made: int = 0
        self.drivers_released: int = 0

        # Simulated driver pool
        self._available_drivers = ["Alice", "Bob", "Carol", "Dave", "Eve"]

    async def handle_order_confirmed(self, topic: str, data: dict) -> None:
        """Handle an order.confirmed event by matching a driver.

        This is triggered when the restaurant confirms the order -- NOT
        when the customer places it. The topic filtering ensures we only
        spend resources on orders that will actually be fulfilled.

        Args:
            topic: The event topic ("order.confirmed").
            data: The event payload -- order details.
        """
        order_id = data.get("order_id", "unknown")

        # --- Idempotency check ---
        if order_id in self._assignments:
            print(
                f"    [Driver Matching] Order {order_id} already assigned to "
                f"{self._assignments[order_id]}, skipping"
            )
            return

        print(f"    [Driver Matching] Finding nearest driver for order {order_id}...")

        # --- Query driver locations ---
        await asyncio.sleep(0.10)  # 100ms -- fetch GPS coordinates from driver app

        # --- Calculate distances ---
        await asyncio.sleep(0.10)  # 100ms -- compute distances, rank by ETA

        # --- Assign closest available driver ---
        await asyncio.sleep(0.10)  # 100ms -- update assignment, notify driver

        if self._available_drivers:
            driver = random.choice(self._available_drivers)
            self._assignments[order_id] = driver
            self.matches_made += 1

            print(
                f"    [Driver Matching] Order {order_id} assigned to driver {driver}!"
            )
            print(
                f"              (matching took 300ms -- customer did NOT wait for this)"
            )
            print(
                f"              Note: this only ran AFTER restaurant confirmed the order."
            )
            print(
                f"              In sequential mode, driver matching runs even if "
                f"restaurant rejects."
            )
        else:
            print(
                f"    [Driver Matching] No available drivers for order {order_id}! "
                f"Queued for retry."
            )

    async def handle_order_cancelled(self, topic: str, data: dict) -> None:
        """Handle an order.cancelled event by releasing the assigned driver.

        This demonstrates multi-topic subscription. The same service reacts
        to both confirmations (assign a driver) and cancellations (release
        the driver). Each is an independent subscription.

        Args:
            topic: The event topic ("order.cancelled").
            data: The event payload.
        """
        order_id = data.get("order_id", "unknown")

        if order_id in self._assignments:
            driver = self._assignments.pop(order_id)
            self.drivers_released += 1
            print(
                f"    [Driver Matching] Order {order_id} cancelled. "
                f"Released driver {driver} back to pool."
            )
        else:
            print(
                f"    [Driver Matching] Order {order_id} cancelled. "
                f"No driver was assigned (order was not yet confirmed)."
            )
