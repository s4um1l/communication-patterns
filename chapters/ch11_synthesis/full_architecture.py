"""Chapter 11 -- Full FoodDash Architecture Simulation

This is the capstone demo. It runs the ENTIRE FoodDash order lifecycle,
using every communication pattern from the course. At each step, it tells
you which pattern is in play and why.

No external servers needed -- everything runs in one process using asyncio
to simulate the distributed system.

Run with:
    uv run python -m chapters.ch11_synthesis.full_architecture
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from shared.models import (
    Customer,
    Driver,
    MenuItem,
    Order,
    OrderItem,
    OrderStatus,
    Restaurant,
)

# ---------------------------------------------------------------------------
# Terminal formatting
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
RED = "\033[31m"
WHITE = "\033[97m"
BG_DARK = "\033[48;5;236m"

PATTERN_COLORS = {
    "Request-Response": GREEN,
    "SSE": CYAN,
    "WebSocket": MAGENTA,
    "Push Notification": YELLOW,
    "Pub/Sub": BLUE,
    "Stateless": WHITE,
    "Multiplexing": RED,
    "Sidecar": DIM,
}


def banner(text: str) -> None:
    width = 74
    print()
    print(f"{BOLD}{'=' * width}")
    print(f"  {text}")
    print(f"{'=' * width}{RESET}")


def pattern_label(name: str, chapter: str) -> str:
    color = PATTERN_COLORS.get(name, WHITE)
    return f"{color}{BOLD}[{name} -- {chapter}]{RESET}"


def step(pattern_name: str, chapter: str, description: str) -> None:
    label = pattern_label(pattern_name, chapter)
    print(f"\n  {label}")
    print(f"  {description}")


def timing(label: str, elapsed_ms: float) -> None:
    bar_len = min(int(elapsed_ms / 10), 40)
    bar = "=" * bar_len + ">"
    print(f"    {DIM}{elapsed_ms:7.1f} ms{RESET}  {GREEN}{bar}{RESET} {label}")


def event_log(source: str, message: str) -> None:
    print(f"    {DIM}[{source}]{RESET} {message}")


# ---------------------------------------------------------------------------
# Simulated infrastructure components
# ---------------------------------------------------------------------------


@dataclass
class SidecarProxy:
    """Ch10 -- Sidecar: handles auth, logging, rate-limiting for every service."""

    name: str
    request_count: int = 0
    total_overhead_ms: float = 0.0

    async def intercept(self, request_desc: str) -> float:
        """Simulate sidecar processing. Returns overhead in ms."""
        self.request_count += 1

        # Auth check
        auth_ms = random.uniform(0.5, 1.5)
        await asyncio.sleep(auth_ms / 1000)

        # Logging
        log_ms = random.uniform(0.1, 0.3)
        await asyncio.sleep(log_ms / 1000)

        # Rate limit check
        rate_ms = random.uniform(0.2, 0.5)
        await asyncio.sleep(rate_ms / 1000)

        overhead = auth_ms + log_ms + rate_ms
        self.total_overhead_ms += overhead
        return overhead


@dataclass
class EventBus:
    """Ch07 -- Pub/Sub broker: routes events to subscribers."""

    subscribers: dict[str, list[Callable]] = field(default_factory=dict)
    event_log: list[dict] = field(default_factory=list)

    def subscribe(self, topic: str, handler: Callable) -> None:
        self.subscribers.setdefault(topic, []).append(handler)

    async def publish(self, topic: str, data: dict) -> int:
        """Publish an event. Returns the number of subscribers notified."""
        event = {"topic": topic, "data": data, "timestamp": time.time()}
        self.event_log.append(event)

        handlers = self.subscribers.get(topic, [])
        for handler in handlers:
            await handler(data)

        return len(handlers)


@dataclass
class SSEStream:
    """Ch04 -- SSE: server-sent events stream for a single client."""

    client_id: str
    events: list[dict] = field(default_factory=list)

    async def push(self, event_type: str, data: dict) -> None:
        event = {
            "id": len(self.events) + 1,
            "type": event_type,
            "data": data,
            "timestamp": time.time(),
        }
        self.events.append(event)
        event_log("SSE", f"Event #{event['id']} -> {self.client_id}: {event_type}")


@dataclass
class WebSocketConnection:
    """Ch05 -- WebSocket: bidirectional connection between customer and driver."""

    party_a: str
    party_b: str
    messages: list[dict] = field(default_factory=list)

    async def send(self, sender: str, text: str) -> None:
        msg = {"sender": sender, "text": text, "timestamp": time.time()}
        self.messages.append(msg)
        receiver = self.party_b if sender == self.party_a else self.party_a
        event_log("WebSocket", f"{sender} -> {receiver}: \"{text}\"")


@dataclass
class PushService:
    """Ch06 -- Push Notifications: fire-and-forget to offline devices."""

    notifications_sent: list[dict] = field(default_factory=list)

    async def send(self, device_token: str, title: str, body: str) -> None:
        # Simulate platform delivery delay
        delay_ms = random.uniform(100, 500)
        await asyncio.sleep(delay_ms / 1000)
        notification = {
            "device_token": device_token,
            "title": title,
            "body": body,
            "delay_ms": delay_ms,
        }
        self.notifications_sent.append(notification)
        event_log("Push", f"-> {device_token[:12]}...: \"{title}: {body}\" ({delay_ms:.0f}ms platform delay)")


@dataclass
class MultiplexedConnection:
    """Ch09 -- Multiplexing: single TCP connection carrying multiple streams."""

    connection_id: str
    active_streams: dict[int, str] = field(default_factory=dict)
    next_stream_id: int = 1

    def open_stream(self, purpose: str) -> int:
        stream_id = self.next_stream_id
        self.next_stream_id += 2  # HTTP/2 uses odd IDs for client-initiated
        self.active_streams[stream_id] = purpose
        return stream_id

    def close_stream(self, stream_id: int) -> None:
        self.active_streams.pop(stream_id, None)


# ---------------------------------------------------------------------------
# Simulated services
# ---------------------------------------------------------------------------


class OrderService:
    """Handles order CRUD -- the core of FoodDash."""

    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.sidecar = SidecarProxy(name="order-sidecar")

    async def create_order(self, order: Order) -> Order:
        overhead = await self.sidecar.intercept(f"POST /orders/{order.id}")
        # Simulate DB write
        await asyncio.sleep(random.uniform(5, 15) / 1000)
        self.orders[order.id] = order
        return order

    async def update_status(self, order_id: str, status: OrderStatus) -> Order:
        overhead = await self.sidecar.intercept(f"PATCH /orders/{order_id}")
        await asyncio.sleep(random.uniform(2, 8) / 1000)
        order = self.orders[order_id]
        order.status = status
        order.updated_at = time.time()
        return order


class KitchenService:
    """Subscribes to order events, prepares food."""

    def __init__(self) -> None:
        self.sidecar = SidecarProxy(name="kitchen-sidecar")
        self.orders_received: list[str] = []

    async def handle_order_placed(self, data: dict) -> None:
        await self.sidecar.intercept("event:order.placed")
        self.orders_received.append(data["order_id"])
        event_log("Kitchen", f"Received order {data['order_id']} -- starting preparation")


class BillingService:
    """Subscribes to order events, processes payment."""

    def __init__(self) -> None:
        self.sidecar = SidecarProxy(name="billing-sidecar")
        self.charges: list[dict] = []

    async def handle_order_placed(self, data: dict) -> None:
        await self.sidecar.intercept("event:order.placed")
        charge = {"order_id": data["order_id"], "amount_cents": data["total_cents"]}
        self.charges.append(charge)
        event_log("Billing", f"Charged ${data['total_cents'] / 100:.2f} for order {data['order_id']}")


class DriverMatchService:
    """Subscribes to order events, assigns a driver."""

    def __init__(self) -> None:
        self.sidecar = SidecarProxy(name="driver-match-sidecar")
        self.assignments: dict[str, str] = {}

    async def handle_order_confirmed(self, data: dict) -> str | None:
        await self.sidecar.intercept("event:order.confirmed")
        driver_id = f"driver_{random.randint(100, 999)}"
        self.assignments[data["order_id"]] = driver_id
        event_log("DriverMatch", f"Assigned driver {driver_id} to order {data['order_id']}")
        return driver_id


class NotificationService:
    """Subscribes to events, routes to SSE/Push/WebSocket as appropriate."""

    def __init__(self) -> None:
        self.sidecar = SidecarProxy(name="notification-sidecar")
        self.notifications_routed: int = 0

    async def handle_event(self, data: dict) -> None:
        await self.sidecar.intercept(f"event:{data.get('event_type', 'unknown')}")
        self.notifications_routed += 1


# ---------------------------------------------------------------------------
# The simulation
# ---------------------------------------------------------------------------


async def simulate_full_lifecycle() -> None:
    """Run the complete FoodDash order lifecycle, demonstrating every pattern."""

    banner("FoodDash Full Architecture Simulation")
    print(f"\n  This simulation runs the complete order lifecycle using all 10")
    print(f"  communication patterns. Each step labels the pattern in use.\n")

    overall_start = time.time()

    # --- Setup ---

    restaurant = Restaurant(
        name="Burger Palace",
        menu=[
            MenuItem(name="Classic Burger", price_cents=899),
            MenuItem(name="Truffle Fries", price_cents=599),
            MenuItem(name="Milkshake", price_cents=499),
        ],
    )

    customer = Customer(name="Alice", address="742 Evergreen Terrace")
    driver = Driver(name="Bob", latitude=37.7749, longitude=-122.4194)

    order = Order(
        customer=customer,
        restaurant_id=restaurant.id,
        items=[
            OrderItem(menu_item=restaurant.menu[0], quantity=2),
            OrderItem(menu_item=restaurant.menu[1], quantity=1),
        ],
    )

    # --- Infrastructure ---

    event_bus = EventBus()
    order_service = OrderService()
    kitchen_service = KitchenService()
    billing_service = BillingService()
    driver_match_service = DriverMatchService()
    notification_service = NotificationService()

    sse_stream = SSEStream(client_id=customer.id)
    ws_connection = WebSocketConnection(party_a=customer.name, party_b=driver.name)
    push_service = PushService()

    # Subscribe services to event bus
    event_bus.subscribe("order.placed", kitchen_service.handle_order_placed)
    event_bus.subscribe("order.placed", billing_service.handle_order_placed)
    event_bus.subscribe("order.confirmed", driver_match_service.handle_order_confirmed)

    # For all order events, route through notification service
    for topic in ["order.placed", "order.confirmed", "order.preparing",
                  "order.ready", "order.picked_up", "order.en_route", "order.delivered"]:
        event_bus.subscribe(topic, notification_service.handle_event)

    phase_times: list[tuple[str, float]] = []

    # =========================================================================
    # PHASE 1: Customer places order (Request-Response + Stateless + Sidecar)
    # =========================================================================

    banner("Phase 1: Customer Places Order")

    step("Multiplexing", "Ch09",
         "Customer's app opens a single HTTP/2 connection to the API gateway.")
    mux = MultiplexedConnection(connection_id=f"client_{customer.id}")
    stream_api = mux.open_stream("API requests")
    stream_sse = mux.open_stream("SSE event stream")
    print(f"    Connection {mux.connection_id}: stream {stream_api} (API), stream {stream_sse} (SSE)")

    step("Stateless", "Ch08",
         "API Gateway is stateless -- any instance can handle this request.")
    event_log("Gateway", "Request routed to gateway-instance-3 (no sticky session needed)")

    step("Sidecar", "Ch10",
         "Sidecar proxy intercepts the request before it reaches the order service.")

    t0 = time.time()

    step("Request-Response", "Ch01",
         f"POST /orders -- Customer places order for {len(order.items)} items, ${order.total_cents / 100:.2f}")
    t1 = time.time()
    created_order = await order_service.create_order(order)
    t2 = time.time()
    timing("Order created in DB", (t2 - t1) * 1000)
    event_log("Response", f"201 Created -- order_id={created_order.id}, status={created_order.status.value}")

    phase_times.append(("Place Order", (t2 - t0) * 1000))

    # =========================================================================
    # PHASE 2: Event fan-out (Pub/Sub + Sidecar)
    # =========================================================================

    banner("Phase 2: Event Fan-Out")

    step("Pub/Sub", "Ch07",
         "Order service publishes 'order.placed' -- multiple services react independently.")

    t0 = time.time()
    event_data = {
        "order_id": created_order.id,
        "customer_id": customer.id,
        "restaurant_id": restaurant.id,
        "total_cents": created_order.total_cents,
        "event_type": "order.placed",
    }
    sub_count = await event_bus.publish("order.placed", event_data)
    t1 = time.time()
    timing(f"Event delivered to {sub_count} subscribers", (t1 - t0) * 1000)

    step("Sidecar", "Ch10",
         "Each subscriber's sidecar verified auth and logged the event.")
    total_sidecar_reqs = sum([
        kitchen_service.sidecar.request_count,
        billing_service.sidecar.request_count,
        notification_service.sidecar.request_count,
    ])
    print(f"    Sidecar requests so far: {total_sidecar_reqs} (across all services)")

    phase_times.append(("Event Fan-Out", (t1 - t0) * 1000))

    # =========================================================================
    # PHASE 3: SSE updates to customer (SSE)
    # =========================================================================

    banner("Phase 3: Real-Time Status Updates")

    step("SSE", "Ch04",
         "Customer's app receives status updates via Server-Sent Events stream.")

    t0 = time.time()
    statuses = [
        (OrderStatus.CONFIRMED, "Restaurant confirmed your order"),
        (OrderStatus.PREPARING, "Kitchen is preparing your food"),
        (OrderStatus.READY, "Your order is ready for pickup"),
    ]

    for new_status, description in statuses:
        await order_service.update_status(created_order.id, new_status)
        await sse_stream.push("order_status", {
            "order_id": created_order.id,
            "status": new_status.value,
            "message": description,
        })

        # Publish status change to event bus
        await event_bus.publish(f"order.{new_status.value}", {
            "order_id": created_order.id,
            "status": new_status.value,
            "event_type": f"order.{new_status.value}",
        })

        # Simulate time between status changes
        await asyncio.sleep(random.uniform(10, 30) / 1000)

    t1 = time.time()
    timing(f"Streamed {len(statuses)} status updates via SSE", (t1 - t0) * 1000)

    phase_times.append(("SSE Status Updates", (t1 - t0) * 1000))

    # =========================================================================
    # PHASE 4: Driver assignment + WebSocket chat
    # =========================================================================

    banner("Phase 4: Driver Assigned + Real-Time Chat")

    step("WebSocket", "Ch05",
         f"Bidirectional WebSocket opened between {customer.name} and {driver.name}.")

    t0 = time.time()
    await ws_connection.send(driver.name, "Hi Alice! I'm on my way to Burger Palace.")
    await asyncio.sleep(5 / 1000)
    await ws_connection.send(customer.name, "Great! I'm at the front door.")
    await asyncio.sleep(3 / 1000)
    await ws_connection.send(driver.name, "Perfect, see you in 10 minutes!")
    t1 = time.time()
    timing(f"Exchanged {len(ws_connection.messages)} messages", (t1 - t0) * 1000)

    # Driver picks up
    await order_service.update_status(created_order.id, OrderStatus.PICKED_UP)
    await sse_stream.push("order_status", {
        "order_id": created_order.id,
        "status": "picked_up",
        "message": f"{driver.name} picked up your order",
    })
    await event_bus.publish("order.picked_up", {
        "order_id": created_order.id,
        "status": "picked_up",
        "event_type": "order.picked_up",
    })

    # En route
    await order_service.update_status(created_order.id, OrderStatus.EN_ROUTE)
    await sse_stream.push("order_status", {
        "order_id": created_order.id,
        "status": "en_route",
        "message": f"{driver.name} is on the way!",
    })
    await event_bus.publish("order.en_route", {
        "order_id": created_order.id,
        "status": "en_route",
        "event_type": "order.en_route",
    })

    phase_times.append(("WebSocket Chat", (t1 - t0) * 1000))

    # =========================================================================
    # PHASE 5: Push notification (customer locks phone)
    # =========================================================================

    banner("Phase 5: Push Notification (Customer Offline)")

    step("Push Notification", "Ch06",
         "Customer locked their phone. Push notification bridges the gap.")

    t0 = time.time()
    await push_service.send(
        device_token=f"apns_{customer.id}_{'x' * 20}",
        title="FoodDash",
        body=f"{driver.name} is arriving with your order!",
    )
    t1 = time.time()
    timing("Push notification delivered via APNs", (t1 - t0) * 1000)

    phase_times.append(("Push Notification", (t1 - t0) * 1000))

    # =========================================================================
    # PHASE 6: Delivery complete
    # =========================================================================

    banner("Phase 6: Order Delivered")

    step("Request-Response", "Ch01",
         "Driver confirms delivery via POST /orders/{id}/deliver")

    t0 = time.time()
    await order_service.update_status(created_order.id, OrderStatus.DELIVERED)
    t1 = time.time()

    step("Pub/Sub", "Ch07",
         "order.delivered event published -- analytics, loyalty, and feedback services react.")

    sub_count = await event_bus.publish("order.delivered", {
        "order_id": created_order.id,
        "status": "delivered",
        "event_type": "order.delivered",
    })
    t2 = time.time()
    timing("Order marked delivered", (t1 - t0) * 1000)
    timing(f"Delivered event sent to {sub_count} subscribers", (t2 - t1) * 1000)

    step("SSE", "Ch04",
         "Final SSE event pushed to customer (if they re-open the app).")
    await sse_stream.push("order_status", {
        "order_id": created_order.id,
        "status": "delivered",
        "message": "Your order has been delivered. Enjoy!",
    })

    step("Multiplexing", "Ch09",
         "Closing streams on the multiplexed connection.")
    mux.close_stream(stream_api)
    mux.close_stream(stream_sse)
    print(f"    Active streams remaining: {len(mux.active_streams)}")

    phase_times.append(("Delivery", (t2 - t0) * 1000))

    overall_end = time.time()

    # =========================================================================
    # Summary
    # =========================================================================

    banner("Simulation Complete -- Summary")

    print(f"\n  {BOLD}Order Lifecycle{RESET}")
    print(f"  Order ID:    {created_order.id}")
    print(f"  Customer:    {customer.name}")
    print(f"  Restaurant:  {restaurant.name}")
    print(f"  Items:       {len(created_order.items)} ({created_order.total_cents / 100:.2f})")
    print(f"  Final status: {created_order.status.value}")

    print(f"\n  {BOLD}Timing by Phase{RESET}")
    for phase_name, phase_ms in phase_times:
        bar_len = min(int(phase_ms / 5), 50)
        bar = "=" * bar_len + ">"
        print(f"    {phase_ms:7.1f} ms  {GREEN}{bar}{RESET} {phase_name}")
    total_ms = (overall_end - overall_start) * 1000
    print(f"    {'─' * 60}")
    print(f"    {total_ms:7.1f} ms  TOTAL")

    print(f"\n  {BOLD}Patterns Used{RESET}")
    patterns_used = [
        ("Request-Response", "Ch01", "Order placement + delivery confirmation"),
        ("SSE", "Ch04", f"{len(sse_stream.events)} status events streamed"),
        ("WebSocket", "Ch05", f"{len(ws_connection.messages)} chat messages exchanged"),
        ("Push Notification", "Ch06", f"{len(push_service.notifications_sent)} notification(s) sent"),
        ("Pub/Sub", "Ch07", f"{len(event_bus.event_log)} events published across {len(event_bus.subscribers)} topics"),
        ("Stateless", "Ch08", "API Gateway -- no sticky sessions"),
        ("Multiplexing", "Ch09", f"1 TCP connection, {mux.next_stream_id // 2} streams used"),
        ("Sidecar", "Ch10", f"{sum_sidecar_requests(order_service, kitchen_service, billing_service, driver_match_service, notification_service)} sidecar interceptions"),
    ]

    for name, ch, desc in patterns_used:
        label = pattern_label(name, ch)
        print(f"    {label} {desc}")

    print(f"\n  {BOLD}Infrastructure Stats{RESET}")
    print(f"    Event bus: {len(event_bus.event_log)} events, {sum(len(v) for v in event_bus.subscribers.values())} total subscriptions")
    print(f"    SSE stream: {len(sse_stream.events)} events delivered")
    print(f"    WebSocket: {len(ws_connection.messages)} messages (bidirectional)")
    print(f"    Push: {len(push_service.notifications_sent)} notifications")
    print(f"    Sidecar overhead: {sum_sidecar_overhead(order_service, kitchen_service, billing_service, driver_match_service, notification_service):.1f} ms total across all services")

    print()


def sum_sidecar_requests(*services: Any) -> int:
    return sum(s.sidecar.request_count for s in services)


def sum_sidecar_overhead(*services: Any) -> float:
    return sum(s.sidecar.total_overhead_ms for s in services)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the full architecture simulation."""
    asyncio.run(simulate_full_lifecycle())


if __name__ == "__main__":
    main()
