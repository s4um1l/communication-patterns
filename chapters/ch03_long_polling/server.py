"""Chapter 03 — Long Polling: FoodDash Order Server

The key evolution from Ch01/Ch02: instead of responding immediately to status
queries, this server HOLDS the request open until the status actually changes
(or a timeout expires). The client gets near-instant notification without
burning thousands of wasted requests.

The implementation hinges on asyncio.Event — one event per order. When a
long poll request arrives, the handler suspends on event.wait(). When the
status is advanced, the event is set, waking all waiting coroutines.

Key things to notice:
    - The /poll endpoint uses asyncio.wait_for with a timeout
    - Each order gets its own asyncio.Event for signaling changes
    - The /advance endpoint BOTH updates the status AND notifies waiters
    - We track how many clients are currently waiting (held connections)
    - This MUST be async — a sync server would hold a thread per waiter

Run with:
    uv run uvicorn chapters.ch03_long_polling.server:app --port 8003
"""

from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from shared.db import DB
from shared.models import Customer, MenuItem, Order, OrderItem, OrderStatus

# ---------------------------------------------------------------------------
# App & database
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FoodDash — Ch03 Long Polling",
    description=(
        "The server holds requests open until data changes. "
        "Clients get near-instant updates without wasting requests."
    ),
    version="0.1.0",
)

db = DB()

# ---------------------------------------------------------------------------
# Long polling infrastructure
#
# Each order gets an asyncio.Event. When a long poll request arrives for an
# order whose status hasn't changed, the handler awaits the event. When the
# status is advanced, we set the event (waking all waiters) and then
# immediately replace it with a fresh event for the next round of waiters.
# ---------------------------------------------------------------------------

# order_id -> asyncio.Event
_order_events: dict[str, asyncio.Event] = {}

# Stats for educational output
_stats = {
    "waiting_connections": 0,
    "total_polls_received": 0,
    "total_notifications_sent": 0,
    "total_timeouts": 0,
    "total_immediate_responses": 0,
}


def _get_event(order_id: str) -> asyncio.Event:
    """Get or create the event for an order. Thread-safe within the event loop."""
    if order_id not in _order_events:
        _order_events[order_id] = asyncio.Event()
    return _order_events[order_id]


def _reset_event(order_id: str) -> None:
    """Replace the event with a fresh one after notifying waiters.

    We can't just call event.clear() because there's a race condition:
    a new waiter might arrive between set() and clear(), missing the
    notification. Creating a new event avoids this entirely.
    """
    _order_events[order_id] = asyncio.Event()


def _print_stats(context: str) -> None:
    """Print server stats for educational visibility."""
    print(
        f"  [{context}] "
        f"waiting={_stats['waiting_connections']} | "
        f"polls={_stats['total_polls_received']} | "
        f"notifications={_stats['total_notifications_sent']} | "
        f"timeouts={_stats['total_timeouts']} | "
        f"immediate={_stats['total_immediate_responses']}"
    )


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PlaceOrderRequest(BaseModel):
    customer_name: str = Field(..., min_length=1, examples=["Alice"])
    restaurant_id: str = Field(..., examples=["rest_01"])
    item_ids: list[str] = Field(..., min_length=1, examples=[["item_01", "item_02"]])


class PlaceOrderResponse(BaseModel):
    order_id: str
    status: OrderStatus
    items: list[dict]
    total_cents: int
    created_at: float


class OrderResponse(BaseModel):
    order_id: str
    customer_name: str
    restaurant_id: str
    status: OrderStatus
    items: list[dict]
    total_cents: int
    created_at: float
    updated_at: float


class PollResponse(BaseModel):
    """Response from the long poll endpoint.

    changed=True means the status differs from what the client last saw.
    changed=False means the timeout expired with no change.
    """
    order_id: str
    status: OrderStatus
    changed: bool
    updated_at: float
    server_held_seconds: float  # How long the server held the request


class AdvanceResponse(BaseModel):
    order_id: str
    old_status: OrderStatus
    new_status: OrderStatus
    waiters_notified: int


class StatsResponse(BaseModel):
    waiting_connections: int
    total_polls_received: int
    total_notifications_sent: int
    total_timeouts: int
    total_immediate_responses: int


# ---------------------------------------------------------------------------
# Helper to build order item dicts
# ---------------------------------------------------------------------------

def _order_items_to_dicts(order: Order) -> list[dict]:
    return [
        {
            "name": oi.menu_item.name,
            "quantity": oi.quantity,
            "subtotal_cents": oi.subtotal_cents,
        }
        for oi in order.items
    ]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/orders", response_model=PlaceOrderResponse, status_code=201)
async def place_order(req: PlaceOrderRequest) -> PlaceOrderResponse:
    """Place a new order. Same as Ch01 — this endpoint is unchanged.

    The interesting part is what happens AFTER: the client can now long-poll
    for status changes instead of hammering GET /orders/{id} every 3 seconds.
    """
    restaurant = db.get_restaurant(req.restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail=f"Restaurant '{req.restaurant_id}' not found")

    menu_by_id: dict[str, MenuItem] = {item.id: item for item in restaurant.menu}
    order_items: list[OrderItem] = []
    for item_id in req.item_ids:
        menu_item = menu_by_id.get(item_id)
        if menu_item is None:
            raise HTTPException(
                status_code=422,
                detail=f"Item '{item_id}' not found in restaurant '{restaurant.name}'",
            )
        existing = next((oi for oi in order_items if oi.menu_item.id == item_id), None)
        if existing:
            existing.quantity += 1
        else:
            order_items.append(OrderItem(menu_item=menu_item, quantity=1))

    customer = Customer(name=req.customer_name)
    order = Order(
        customer=customer,
        restaurant_id=req.restaurant_id,
        items=order_items,
        status=OrderStatus.PLACED,
    )
    await db.place_order(order)

    print(f"  [ORDER PLACED] id={order.id} customer={req.customer_name}")

    return PlaceOrderResponse(
        order_id=order.id,
        status=order.status,
        items=_order_items_to_dicts(order),
        total_cents=order.total_cents,
        created_at=order.created_at,
    )


@app.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(order_id: str) -> OrderResponse:
    """Standard GET — returns current status immediately. No long polling.

    This still exists for clients that just want a quick status check
    without waiting. The long poll endpoint is /orders/{id}/poll.
    """
    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found")
    return OrderResponse(
        order_id=order.id,
        customer_name=order.customer.name,
        restaurant_id=order.restaurant_id,
        status=order.status,
        items=_order_items_to_dicts(order),
        total_cents=order.total_cents,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


@app.get("/orders/{order_id}/poll")
async def poll_order(
    order_id: str,
    timeout: int = 30,
    last_status: str | None = None,
    response: Response = None,
) -> PollResponse:
    """Long poll for order status changes.

    This is the core of long polling. The mechanics:

    1. If last_status is None or differs from current status → respond immediately
       (the client is either starting fresh or missed a change while reconnecting)

    2. If last_status matches current status → HOLD the request:
       - Create/get an asyncio.Event for this order
       - await event.wait() with the specified timeout
       - If the event fires (status changed) → respond with new status
       - If timeout expires → respond with 304 Not Modified

    The timeout parameter should be LESS than your load balancer's idle
    timeout (e.g., 30s vs ALB's 60s default) to prevent the LB from
    killing the connection before we respond.
    """
    _stats["total_polls_received"] += 1

    # Clamp timeout to reasonable bounds
    timeout = max(1, min(timeout, 55))  # 55s max — stay under ALB's 60s

    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found")

    hold_start = time.time()

    # Case 1: Status already differs from what client knows → respond immediately
    if last_status is None or order.status.value != last_status:
        _stats["total_immediate_responses"] += 1
        print(
            f"  [POLL IMMEDIATE] order={order_id} "
            f"client_knows={last_status} current={order.status.value}"
        )
        _print_stats("POLL")
        return PollResponse(
            order_id=order.id,
            status=order.status,
            changed=True,
            updated_at=order.updated_at,
            server_held_seconds=0.0,
        )

    # Case 2: No change yet — HOLD the request open
    event = _get_event(order_id)
    _stats["waiting_connections"] += 1
    print(
        f"  [POLL HOLD] order={order_id} "
        f"status={order.status.value} timeout={timeout}s "
        f"(now holding {_stats['waiting_connections']} connections)"
    )

    try:
        # This is where the magic happens. The coroutine SUSPENDS here.
        # No CPU is consumed. No thread is blocked. The event loop is free
        # to handle other requests. This coroutine only wakes when either:
        #   (a) event.set() is called (status changed), or
        #   (b) the timeout expires
        await asyncio.wait_for(event.wait(), timeout=timeout)

        # Event fired — status changed! Fetch the updated order.
        order = await db.get_order(order_id)
        held_seconds = time.time() - hold_start
        _stats["waiting_connections"] -= 1
        _stats["total_notifications_sent"] += 1

        print(
            f"  [POLL NOTIFY] order={order_id} "
            f"new_status={order.status.value} held={held_seconds:.2f}s"
        )
        _print_stats("NOTIFY")

        return PollResponse(
            order_id=order.id,
            status=order.status,
            changed=True,
            updated_at=order.updated_at,
            server_held_seconds=round(held_seconds, 3),
        )

    except asyncio.TimeoutError:
        # Timeout expired — no change. Respond so the client can reconnect.
        held_seconds = time.time() - hold_start
        _stats["waiting_connections"] -= 1
        _stats["total_timeouts"] += 1

        print(
            f"  [POLL TIMEOUT] order={order_id} "
            f"status={order.status.value} held={held_seconds:.2f}s"
        )
        _print_stats("TIMEOUT")

        # We return 200 with changed=False rather than 304 to keep the
        # response body consistent. In production you might use 304.
        return PollResponse(
            order_id=order.id,
            status=order.status,
            changed=False,
            updated_at=order.updated_at,
            server_held_seconds=round(held_seconds, 3),
        )


@app.post("/orders/{order_id}/advance", response_model=AdvanceResponse)
async def advance_order(order_id: str) -> AdvanceResponse:
    """Advance an order to its next status AND notify waiting long pollers.

    This is the "write" side of long polling. When a status changes:
    1. Update the order in the database
    2. Set the asyncio.Event for this order (wakes all waiting coroutines)
    3. Replace the event with a fresh one for future waiters

    The event.set() call is what makes long polling work: it instantly wakes
    every coroutine that is suspended on event.wait() for this order.
    """
    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found")

    old_status = order.status

    # Advance the status in the database
    updated_order = await db.update_order_status(order_id)
    if updated_order is None:
        raise HTTPException(status_code=400, detail="Cannot advance order status")

    new_status = updated_order.status

    # Notify all waiting long pollers for this order
    event = _get_event(order_id)

    # Count how many coroutines are waiting on this event.
    # asyncio.Event doesn't expose this directly, but we track it via _stats.
    waiters = _stats["waiting_connections"]  # Approximate — includes all orders

    event.set()  # Wake all waiters — this is the notification mechanism
    _reset_event(order_id)  # Fresh event for future waiters

    print(
        f"  [ADVANCE] order={order_id} "
        f"{old_status.value} -> {new_status.value} "
        f"(~{waiters} total waiting connections)"
    )

    return AdvanceResponse(
        order_id=order_id,
        old_status=old_status,
        new_status=new_status,
        waiters_notified=waiters,
    )


@app.get("/stats", response_model=StatsResponse)
async def get_stats() -> StatsResponse:
    """Server stats — see how many connections are being held open."""
    return StatsResponse(**_stats)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "chapter": "03-long-polling",
        "timestamp": time.time(),
        "waiting_connections": _stats["waiting_connections"],
    }
