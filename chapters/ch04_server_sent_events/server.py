"""Chapter 04 — Server-Sent Events: FoodDash Order Server

The key evolution from Ch03: instead of delivering ONE event per request-response
cycle (long polling), we keep a single HTTP response open and stream MULTIPLE
events over it. The restaurant dashboard gets a continuous feed of all incoming
orders and status changes without ever reconnecting.

The implementation uses:
    - asyncio.Queue per connected SSE client for fan-out
    - Event IDs for automatic resumption via Last-Event-ID
    - Heartbeat comments every 15 seconds to keep connections alive through proxies
    - sse-starlette for clean SSE response formatting

Key things to notice:
    - The /orders/stream endpoint never returns — it yields events forever
    - Each connected client gets its own asyncio.Queue
    - When an event occurs, it is pushed to ALL connected client queues
    - The server tracks an event log for replay on reconnection (Last-Event-ID)
    - Heartbeats are comment lines (`:`) that proxies see as activity

Run with:
    uv run uvicorn chapters.ch04_server_sent_events.server:app --port 8004
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from shared.db import DB
from shared.models import Customer, MenuItem, Order, OrderItem, OrderStatus

# ---------------------------------------------------------------------------
# App & database
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FoodDash — Ch04 Server-Sent Events",
    description=(
        "The server keeps an HTTP response open and streams events to all "
        "connected clients. Multiple events, one connection, zero reconnections."
    ),
    version="0.1.0",
)

db = DB()

# ---------------------------------------------------------------------------
# SSE broadcasting infrastructure
#
# Every connected SSE client gets an asyncio.Queue. When an event occurs
# (new order, status change), we push it to every queue. Each client's
# generator pulls from its queue and yields SSE events.
#
# We also maintain an event log for replay on reconnection. When a client
# reconnects with Last-Event-ID, we replay all events after that ID.
# ---------------------------------------------------------------------------

# All connected client queues — we broadcast to every one
_subscribers: list[asyncio.Queue] = []

# Event log for replay on reconnection
# Each entry: {"id": int, "event": str, "data": str}
_event_log: list[dict] = []
_next_event_id: int = 1

# Stats for educational output
_stats = {
    "connected_clients": 0,
    "total_events_sent": 0,
    "total_orders_placed": 0,
    "total_status_advances": 0,
    "total_reconnections": 0,
    "heartbeats_sent": 0,
}

# Max events to keep in the replay buffer
_MAX_EVENT_LOG = 500


def _broadcast(event_type: str, data: dict) -> int:
    """Push an event to all connected SSE clients and log it for replay.

    Returns the event ID assigned to this event.
    """
    global _next_event_id

    event_id = _next_event_id
    _next_event_id += 1

    # Store in the replay log
    event_record = {
        "id": event_id,
        "event": event_type,
        "data": json.dumps(data),
    }
    _event_log.append(event_record)

    # Trim the log if it gets too large
    if len(_event_log) > _MAX_EVENT_LOG:
        _event_log[:] = _event_log[-_MAX_EVENT_LOG:]

    # Push to every connected client's queue
    for queue in _subscribers:
        try:
            queue.put_nowait(event_record)
        except asyncio.QueueFull:
            # Client is too slow — skip this event for them.
            # In production you might disconnect slow clients.
            print(f"  [WARN] Dropping event {event_id} for slow client (queue full)")

    _stats["total_events_sent"] += len(_subscribers)

    print(
        f"  [BROADCAST] event={event_type} id={event_id} "
        f"-> {len(_subscribers)} client(s)"
    )
    return event_id


def _get_events_after(last_event_id: int) -> list[dict]:
    """Get all events with ID > last_event_id from the replay log."""
    return [e for e in _event_log if e["id"] > last_event_id]


def _print_stats(context: str) -> None:
    print(
        f"  [{context}] "
        f"clients={_stats['connected_clients']} | "
        f"events_sent={_stats['total_events_sent']} | "
        f"orders={_stats['total_orders_placed']} | "
        f"advances={_stats['total_status_advances']} | "
        f"reconnections={_stats['total_reconnections']} | "
        f"heartbeats={_stats['heartbeats_sent']}"
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


class AdvanceResponse(BaseModel):
    order_id: str
    old_status: OrderStatus
    new_status: OrderStatus
    event_id: int


class StatsResponse(BaseModel):
    connected_clients: int
    total_events_sent: int
    total_orders_placed: int
    total_status_advances: int
    total_reconnections: int
    heartbeats_sent: int


# ---------------------------------------------------------------------------
# Helper
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
# SSE stream endpoint
# ---------------------------------------------------------------------------


@app.get("/orders/stream")
async def order_stream(request: Request):
    """SSE endpoint: streams ALL new orders and status changes.

    This is the heart of Chapter 04. The mechanics:

    1. Client connects with GET /orders/stream
    2. Server responds with Content-Type: text/event-stream
    3. The response NEVER ends — we keep yielding events
    4. Each event has: event type, id (for resumption), data (JSON)
    5. Every 15 seconds, we send a comment (: heartbeat) to keep alive
    6. If the client reconnects with Last-Event-ID, we replay missed events

    The connection persists until the client disconnects. Multiple events
    flow over this single connection — no reconnection overhead between events.
    """
    # Check for reconnection via Last-Event-ID
    last_event_id_header = request.headers.get("last-event-id")
    last_event_id = 0
    if last_event_id_header:
        try:
            last_event_id = int(last_event_id_header)
            _stats["total_reconnections"] += 1
            print(
                f"  [RECONNECT] Client reconnecting with Last-Event-ID: {last_event_id}"
            )
        except ValueError:
            pass

    # Create a queue for this client
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.append(queue)
    _stats["connected_clients"] += 1

    print(
        f"  [CONNECT] New SSE client (total: {_stats['connected_clients']})"
    )
    _print_stats("STREAM")

    async def event_generator():
        """Yields SSE events. This generator runs for the lifetime of the connection."""
        try:
            # First: replay any missed events if the client is reconnecting
            if last_event_id > 0:
                missed = _get_events_after(last_event_id)
                print(f"  [REPLAY] Sending {len(missed)} missed events (after ID {last_event_id})")
                for event in missed:
                    yield {
                        "event": event["event"],
                        "id": str(event["id"]),
                        "data": event["data"],
                    }

            # Send initial connection event
            yield {
                "event": "connected",
                "data": json.dumps({
                    "message": "SSE stream connected",
                    "timestamp": time.time(),
                    "replay_from": last_event_id,
                }),
            }

            # Main event loop: pull from our queue and yield events.
            # Also send heartbeats every 15 seconds to keep the connection alive.
            while True:
                try:
                    # Wait for an event with a 15-second timeout.
                    # If no event arrives within 15 seconds, we send a heartbeat.
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)

                    yield {
                        "event": event["event"],
                        "id": str(event["id"]),
                        "data": event["data"],
                    }

                except asyncio.TimeoutError:
                    # No events for 15 seconds — send a heartbeat comment.
                    # This keeps proxies and load balancers from killing
                    # the "idle" connection.
                    _stats["heartbeats_sent"] += 1
                    yield {"comment": "heartbeat"}

                except asyncio.CancelledError:
                    # Client disconnected
                    break

        finally:
            # Clean up when the client disconnects
            _subscribers.remove(queue)
            _stats["connected_clients"] -= 1
            print(
                f"  [DISCONNECT] SSE client left (remaining: {_stats['connected_clients']})"
            )
            _print_stats("DISCONNECT")

    return EventSourceResponse(
        event_generator(),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Tell nginx not to buffer
        },
    )


# ---------------------------------------------------------------------------
# Order endpoints
# ---------------------------------------------------------------------------


@app.post("/orders", response_model=PlaceOrderResponse, status_code=201)
async def place_order(req: PlaceOrderRequest) -> PlaceOrderResponse:
    """Place a new order AND broadcast it as an SSE event.

    Every connected dashboard immediately sees this order appear — no polling,
    no reconnection, just a push over the open SSE stream.
    """
    restaurant = db.get_restaurant(req.restaurant_id)
    if restaurant is None:
        raise HTTPException(
            status_code=404, detail=f"Restaurant '{req.restaurant_id}' not found"
        )

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

    _stats["total_orders_placed"] += 1

    # Broadcast to all SSE clients
    event_id = _broadcast("order_placed", {
        "order_id": order.id,
        "customer_name": req.customer_name,
        "restaurant_id": req.restaurant_id,
        "restaurant_name": restaurant.name,
        "status": order.status.value,
        "items": _order_items_to_dicts(order),
        "total_cents": order.total_cents,
        "created_at": order.created_at,
    })

    print(f"  [ORDER PLACED] id={order.id} customer={req.customer_name} event_id={event_id}")

    return PlaceOrderResponse(
        order_id=order.id,
        status=order.status,
        items=_order_items_to_dicts(order),
        total_cents=order.total_cents,
        created_at=order.created_at,
    )


@app.post("/orders/{order_id}/advance", response_model=AdvanceResponse)
async def advance_order(order_id: str) -> AdvanceResponse:
    """Advance an order to its next status AND broadcast the change.

    Every connected SSE client receives the status change event immediately
    on their open stream — no reconnection needed.
    """
    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found")

    old_status = order.status

    updated_order = await db.update_order_status(order_id)
    if updated_order is None:
        raise HTTPException(status_code=400, detail="Cannot advance order status")

    new_status = updated_order.status
    _stats["total_status_advances"] += 1

    # Broadcast to all SSE clients
    event_id = _broadcast("status_changed", {
        "order_id": order_id,
        "old_status": old_status.value,
        "new_status": new_status.value,
        "updated_at": updated_order.updated_at,
    })

    print(
        f"  [ADVANCE] order={order_id} "
        f"{old_status.value} -> {new_status.value} event_id={event_id}"
    )

    return AdvanceResponse(
        order_id=order_id,
        old_status=old_status,
        new_status=new_status,
        event_id=event_id,
    )


@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    """Standard GET — returns current order status immediately."""
    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found")
    return {
        "order_id": order.id,
        "customer_name": order.customer.name,
        "restaurant_id": order.restaurant_id,
        "status": order.status.value,
        "items": _order_items_to_dicts(order),
        "total_cents": order.total_cents,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
    }


@app.get("/orders")
async def list_orders():
    """List all orders."""
    return [
        {
            "order_id": order.id,
            "customer_name": order.customer.name,
            "status": order.status.value,
            "total_cents": order.total_cents,
            "created_at": order.created_at,
        }
        for order in db.orders.values()
    ]


# ---------------------------------------------------------------------------
# Dashboard HTML (served directly for convenience)
# ---------------------------------------------------------------------------


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the restaurant dashboard HTML."""
    dashboard_path = Path(__file__).parent / "dashboard.html"
    return HTMLResponse(content=dashboard_path.read_text())


# ---------------------------------------------------------------------------
# Stats & health
# ---------------------------------------------------------------------------


@app.get("/stats", response_model=StatsResponse)
async def get_stats() -> StatsResponse:
    """Server stats — see how SSE connections are managed."""
    return StatsResponse(**_stats)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "chapter": "04-server-sent-events",
        "timestamp": time.time(),
        "connected_clients": _stats["connected_clients"],
    }
