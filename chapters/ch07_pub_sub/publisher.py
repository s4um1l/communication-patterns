"""Chapter 07 -- Pub/Sub: Order Publisher Demo

This is the main demo script for Chapter 07. It creates an in-process broker,
registers all subscribers, places orders, and demonstrates the core pub/sub
properties:

    1. Publisher returns immediately (does not wait for subscribers)
    2. Subscribers process in parallel (not sequentially)
    3. Error isolation (one subscriber's failure does not affect others)
    4. Fan-out (one event reaches multiple subscribers)
    5. Topic filtering (subscribers only get events they care about)

Run with:
    uv run python -m chapters.ch07_pub_sub.publisher
"""

from __future__ import annotations

import asyncio
import time

from shared.db import DB
from shared.models import Customer, Order, OrderItem, OrderStatus

from .broker import EventBroker
from .subscribers.kitchen import KitchenSubscriber
from .subscribers.billing import BillingSubscriber
from .subscribers.driver_matching import DriverMatchingSubscriber


# ---------------------------------------------------------------------------
# Analytics subscriber (inline — simple enough to not need its own file)
# ---------------------------------------------------------------------------

class AnalyticsSubscriber:
    """Records every order event. Subscribes to order.* (wildcard)."""

    def __init__(self) -> None:
        self.events_recorded: list[dict] = []

    async def handle_event(self, topic: str, data: dict) -> None:
        await asyncio.sleep(0.05)  # 50ms — write to analytics store
        self.events_recorded.append({"topic": topic, **data})
        print(
            f"    [Analytics] Recorded event: {topic} "
            f"(total recorded: {len(self.events_recorded)})"
        )


# ---------------------------------------------------------------------------
# Notification subscriber (inline)
# ---------------------------------------------------------------------------

class NotificationSubscriber:
    """Sends push notifications to customers. Subscribes to order.*."""

    async def handle_event(self, topic: str, data: dict) -> None:
        await asyncio.sleep(0.08)  # 80ms — send push notification
        order_id = data.get("order_id", "unknown")
        status = data.get("status", topic.split(".")[-1])
        print(
            f"    [Notifications] Sent push notification for order {order_id}: {status}"
        )


# ---------------------------------------------------------------------------
# Demo: Sequential (the old way) vs Pub/Sub (the new way)
# ---------------------------------------------------------------------------

async def demo_sequential_approach(db: DB, order: Order) -> float:
    """Simulate the OLD approach: calling each service sequentially.

    This is what FoodDash's place_order endpoint looked like BEFORE pub/sub.
    Each service call blocks the next. The customer waits for ALL of them.
    """
    print("\n" + "=" * 70)
    print("DEMO 1: Sequential Approach (the problem)")
    print("=" * 70)
    print("  The order endpoint calls each service one after another.")
    print("  The customer waits for ALL services to complete.\n")

    start = time.perf_counter()

    # Each of these is a blocking call. The next one cannot start
    # until the previous one finishes.
    print("  [Order Service] Validating order...")
    await asyncio.sleep(0.05)   # 50ms — validate

    print("  [Order Service] Saving to database...")
    await asyncio.sleep(0.03)   # 30ms — save

    print("  [Order Service] Charging payment (BLOCKING)...")
    await asyncio.sleep(0.50)   # 500ms — billing is SLOW

    print("  [Order Service] Notifying kitchen (BLOCKED by billing)...")
    await asyncio.sleep(0.20)   # 200ms — kitchen

    print("  [Order Service] Matching driver (BLOCKED by kitchen)...")
    await asyncio.sleep(0.30)   # 300ms — driver matching

    print("  [Order Service] Sending confirmation...")
    await asyncio.sleep(0.08)   # 80ms — notification

    print("  [Order Service] Recording analytics...")
    await asyncio.sleep(0.05)   # 50ms — analytics

    elapsed = time.perf_counter() - start
    print(f"\n  RESULT: Customer waited {elapsed*1000:.0f}ms for 'Order Confirmed'")
    print(f"  That is {elapsed*1000:.0f}ms of the customer staring at a spinner.")
    return elapsed


async def demo_pubsub_approach(db: DB, order: Order) -> float:
    """Demonstrate the NEW approach: pub/sub with decoupled subscribers.

    The order service does TWO things: save the order and publish the event.
    Everything else happens asynchronously. The customer gets a response
    in milliseconds, not seconds.
    """
    print("\n" + "=" * 70)
    print("DEMO 2: Pub/Sub Approach (the solution)")
    print("=" * 70)
    print("  The order endpoint saves the order and publishes ONE event.")
    print("  Subscribers react independently and in parallel.\n")

    # --- Set up broker and subscribers ---
    broker = EventBroker(name="FoodDash")
    await broker.start()

    kitchen = KitchenSubscriber()
    billing = BillingSubscriber()
    driver_matching = DriverMatchingSubscriber()
    analytics = AnalyticsSubscriber()
    notifications = NotificationSubscriber()

    await broker.subscribe("order.placed", kitchen.handle_order_placed, name="kitchen")
    await broker.subscribe("order.placed", billing.handle_order_placed, name="billing")
    await broker.subscribe("order.confirmed", driver_matching.handle_order_confirmed, name="driver_matching")
    await broker.subscribe("order.cancelled", driver_matching.handle_order_cancelled, name="driver_matching_cancel")
    await broker.subscribe("order.*", analytics.handle_event, name="analytics")
    await broker.subscribe("order.*", notifications.handle_event, name="notifications")

    print(f"\n  Registered {broker.metrics.active_subscribers} subscribers across topics\n")

    # --- Publisher side: this is what the order endpoint does ---
    publish_start = time.perf_counter()

    print("  [Order Service] Validating order...")
    await asyncio.sleep(0.05)   # 50ms — validate (still synchronous)

    print("  [Order Service] Saving to database...")
    await asyncio.sleep(0.03)   # 30ms — save (still synchronous)

    print("  [Order Service] Publishing 'order.placed' event...")
    event = await broker.publish("order.placed", {
        "order_id": order.id,
        "customer_name": order.customer.name,
        "restaurant_id": order.restaurant_id,
        "items": [
            {"name": item.menu_item.name, "quantity": item.quantity}
            for item in order.items
        ],
        "total_cents": order.total_cents,
        "status": order.status.value,
    })

    publish_elapsed = time.perf_counter() - publish_start

    print(f"\n  >>> PUBLISHER RETURNED in {publish_elapsed*1000:.1f}ms <<<")
    print(f"  >>> Customer sees 'Order Confirmed' NOW <<<\n")

    # --- Meanwhile, subscribers are processing in the background ---
    print("  [Background] Subscribers are processing in parallel...\n")

    # Give subscribers time to process (in a real system, this is fire-and-forget)
    subscriber_start = time.perf_counter()
    await asyncio.sleep(0.8)  # Wait long enough for the slowest subscriber (billing: 500ms)
    subscriber_elapsed = time.perf_counter() - subscriber_start

    # --- Now publish a status change ---
    print(f"\n  [Order Service] Order confirmed by restaurant. Publishing status change...")
    await broker.publish("order.confirmed", {
        "order_id": order.id,
        "status": "confirmed",
        "previous_status": "placed",
    })

    # Let the driver matching subscriber process
    await asyncio.sleep(0.5)

    # --- Show metrics ---
    print(f"\n  {'─' * 50}")
    print(f"  BROKER METRICS:")
    metrics = broker.metrics.snapshot()
    for key, value in metrics.items():
        print(f"    {key}: {value}")
    print(f"  QUEUE DEPTHS: {broker.get_queue_depths()}")

    # Cleanup
    await broker.stop()

    return publish_elapsed


async def demo_error_isolation(db: DB, order: Order) -> None:
    """Demonstrate that one subscriber's failure does not affect others.

    This is a critical property of pub/sub. In the sequential approach,
    if billing fails, the kitchen never hears about the order. In pub/sub,
    billing can crash and the kitchen still processes normally.
    """
    print("\n" + "=" * 70)
    print("DEMO 3: Error Isolation")
    print("=" * 70)
    print("  What happens when a subscriber crashes? In the sequential model,")
    print("  everything after the crash is blocked. In pub/sub, other subscribers")
    print("  are completely unaffected.\n")

    broker = EventBroker(name="FoodDash")
    await broker.start()

    # A subscriber that always crashes
    async def crashing_billing(topic: str, data: dict) -> None:
        print("    [Billing] Starting payment processing...")
        await asyncio.sleep(0.05)
        raise RuntimeError("PAYMENT GATEWAY UNREACHABLE -- connection refused")

    # A subscriber that works fine
    async def healthy_kitchen(topic: str, data: dict) -> None:
        await asyncio.sleep(0.20)
        print(f"    [Kitchen] Order {data.get('order_id', '?')} received and displayed!")

    # A subscriber that also works fine
    async def healthy_notifications(topic: str, data: dict) -> None:
        await asyncio.sleep(0.08)
        print(f"    [Notifications] Customer notified about order {data.get('order_id', '?')}")

    await broker.subscribe("order.placed", crashing_billing, name="billing (BROKEN)")
    await broker.subscribe("order.placed", healthy_kitchen, name="kitchen")
    await broker.subscribe("order.placed", healthy_notifications, name="notifications")

    print("  Publishing order.placed (billing subscriber WILL crash)...\n")
    await broker.publish("order.placed", {
        "order_id": order.id,
        "customer_name": order.customer.name,
    })

    await asyncio.sleep(0.5)  # Let subscribers process

    print(f"\n  RESULT:")
    print(f"    Billing CRASHED -- but kitchen and notifications worked fine!")
    print(f"    Messages delivered: {broker.metrics.messages_delivered}")
    print(f"    Messages failed: {broker.metrics.messages_failed}")
    print(f"    In sequential mode, the kitchen would NEVER have gotten this order.")

    await broker.stop()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 70)
    print("Chapter 07: Pub/Sub -- FoodDash Order Processing")
    print("=" * 70)

    # Set up a demo order
    db = DB()
    restaurant = db.get_restaurant("rest_01")
    assert restaurant is not None

    customer = Customer(name="Alice", address="123 Main St")
    order = Order(
        customer=customer,
        restaurant_id=restaurant.id,
        items=[
            OrderItem(menu_item=restaurant.menu[0], quantity=2),  # 2x Classic Burger
            OrderItem(menu_item=restaurant.menu[1], quantity=1),  # 1x Fries
        ],
        status=OrderStatus.PLACED,
    )
    await db.place_order(order)

    print(f"\n  Order created: {order.id}")
    print(f"  Customer: {customer.name}")
    print(f"  Items: {', '.join(f'{i.quantity}x {i.menu_item.name}' for i in order.items)}")
    print(f"  Total: ${order.total_cents / 100:.2f}")

    # --- Demo 1: The old sequential approach ---
    seq_time = await demo_sequential_approach(db, order)

    # --- Demo 2: The new pub/sub approach ---
    pub_time = await demo_pubsub_approach(db, order)

    # --- Demo 3: Error isolation ---
    await demo_error_isolation(db, order)

    # --- Summary ---
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    speedup = seq_time / pub_time if pub_time > 0 else float("inf")
    print(f"  Sequential approach: customer waited {seq_time*1000:.0f}ms")
    print(f"  Pub/Sub approach:    customer waited {pub_time*1000:.1f}ms")
    print(f"  Speedup: {speedup:.0f}x faster for the customer")
    print()
    print("  Key takeaways:")
    print("    1. Publisher returns in ~80ms vs ~1200ms (validate + save + publish)")
    print("    2. Subscribers process in PARALLEL, not sequentially")
    print("    3. Billing crash does NOT block kitchen or notifications")
    print("    4. Adding a new subscriber requires ZERO changes to the publisher")
    print("    5. The broker is the single point of failure -- production needs replication")
    print()


if __name__ == "__main__":
    asyncio.run(main())
