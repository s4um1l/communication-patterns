"""Chapter 07 -- Kitchen Subscriber

The kitchen display system. When a customer places an order, the kitchen
needs to know immediately so they can start preparing the food.

Subscribes to: order.placed
Processing time: ~200ms (validate items, send to kitchen display, print ticket)

Educational notes:
    - This subscriber is IDEMPOTENT. If it receives the same order twice
      (at-least-once delivery), it checks the order_id against a set of
      already-processed orders and skips duplicates.
    - Processing time (200ms) does NOT block the publisher. The customer
      got their confirmation 200ms ago. This is the core pub/sub benefit.
    - If this subscriber crashes, billing and driver matching continue
      normally. Error isolation is automatic.
"""

from __future__ import annotations

import asyncio


class KitchenSubscriber:
    """Kitchen display system -- receives new orders and queues them for preparation."""

    def __init__(self) -> None:
        # Idempotency: track which orders we have already processed.
        # In production, this would be a database check or a Redis set
        # with TTL. In-memory is fine for educational purposes.
        self._processed_orders: set[str] = set()
        self.orders_received: int = 0
        self.duplicates_skipped: int = 0

    async def handle_order_placed(self, topic: str, data: dict) -> None:
        """Handle an order.placed event.

        This is the callback registered with the broker. The broker calls
        this function for every event published to "order.placed".

        Args:
            topic: The event topic (always "order.placed" for this handler).
            data: The event payload -- order details.
        """
        order_id = data.get("order_id", "unknown")

        # --- Idempotency check ---
        # At-least-once delivery means we might see this order again.
        # The order_id is our idempotency key.
        if order_id in self._processed_orders:
            self.duplicates_skipped += 1
            print(
                f"    [Kitchen] DUPLICATE order {order_id} -- "
                f"already on the board, skipping "
                f"(duplicates skipped: {self.duplicates_skipped})"
            )
            return

        # --- Process the order ---
        print(f"    [Kitchen] Received order {order_id}!")

        # Simulate processing time:
        #   - Validate items are available (check inventory)
        #   - Send to kitchen display system
        #   - Print physical ticket for the line cook
        await asyncio.sleep(0.20)  # 200ms

        # Build the display information
        customer = data.get("customer_name", "Unknown")
        items = data.get("items", [])
        item_summary = ", ".join(
            f"{item.get('quantity', 1)}x {item.get('name', '?')}"
            for item in items
        )

        self._processed_orders.add(order_id)
        self.orders_received += 1

        print(
            f"    [Kitchen] Order {order_id} displayed on kitchen board:"
        )
        print(
            f"              Customer: {customer} | Items: {item_summary}"
        )
        print(
            f"              (processing took 200ms -- customer did NOT wait for this)"
        )
