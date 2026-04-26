"""Chapter 07 -- Billing Subscriber

The payment processing service. This is intentionally the SLOWEST subscriber
to demonstrate that slow processing does not block the publisher.

Subscribes to: order.placed
Processing time: ~500ms (fraud check, charge payment, generate receipt)

Educational notes:
    - In the sequential model, billing's 500ms latency is added to the
      customer's wait time. Everyone downstream (kitchen, driver, notifications)
      is blocked waiting for billing to finish.
    - In pub/sub, billing takes 500ms but the customer already got their
      confirmation 500ms ago. The publisher's latency was ~1ms (time to
      write to the broker). Billing's latency is completely invisible
      to the customer.
    - This is the canonical example of WHY pub/sub exists: decouple slow
      operations from the critical path.
    - Billing is also idempotent: charging the same order twice would be
      catastrophic, so we MUST deduplicate.
"""

from __future__ import annotations

import asyncio


class BillingSubscriber:
    """Payment processing -- charges the customer and generates a receipt."""

    def __init__(self) -> None:
        self._processed_orders: set[str] = set()
        self.payments_processed: int = 0
        self.total_charged_cents: int = 0
        self.duplicates_prevented: int = 0

    async def handle_order_placed(self, topic: str, data: dict) -> None:
        """Handle an order.placed event by charging the customer.

        Args:
            topic: The event topic (always "order.placed" for this handler).
            data: The event payload -- order details including total_cents.
        """
        order_id = data.get("order_id", "unknown")
        total_cents = data.get("total_cents", 0)

        # --- Idempotency check (CRITICAL for billing) ---
        # Charging a customer twice for the same order is unacceptable.
        # This is why at-least-once + idempotent handlers is the right
        # pattern. The broker might deliver this event twice. We MUST
        # handle that gracefully.
        if order_id in self._processed_orders:
            self.duplicates_prevented += 1
            print(
                f"    [Billing] DUPLICATE charge attempt for order {order_id} -- "
                f"payment already processed! "
                f"(duplicates prevented: {self.duplicates_prevented})"
            )
            return

        # --- Fraud check ---
        print(f"    [Billing] Running fraud check for order {order_id}...")
        await asyncio.sleep(0.15)  # 150ms -- check against fraud rules

        # --- Charge payment ---
        print(
            f"    [Billing] Charging ${total_cents / 100:.2f} "
            f"for order {order_id}..."
        )
        await asyncio.sleep(0.25)  # 250ms -- payment gateway round-trip

        # --- Generate receipt ---
        print(f"    [Billing] Generating receipt for order {order_id}...")
        await asyncio.sleep(0.10)  # 100ms -- create and store receipt

        self._processed_orders.add(order_id)
        self.payments_processed += 1
        self.total_charged_cents += total_cents

        print(
            f"    [Billing] Payment complete for order {order_id}: "
            f"${total_cents / 100:.2f} charged "
            f"(total processing: 500ms -- customer did NOT wait for this)"
        )
