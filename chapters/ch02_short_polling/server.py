"""Chapter 02 — Short Polling: FoodDash Order Server

Extends Ch01's request-response API with a status advancement endpoint and
request tracking. The server itself is identical to Ch01 from a protocol
perspective — it still only speaks when spoken to. The difference is that NOW
we are measuring how often clients ask.

Key additions over Ch01:
    - POST /orders/{order_id}/advance — simulates order status progression
    - Request counter tracking total vs "useful" polls (where status changed)
    - Periodic stats printing so you can watch the waste accumulate in real time

Run with:
    uv run uvicorn chapters.ch02_short_polling.server:app --port 8002
"""

from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from shared.db import DB
from shared.models import Customer, MenuItem, Order, OrderItem, OrderStatus

# ---------------------------------------------------------------------------
# App & database
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FoodDash — Ch02 Short Polling",
    description=(
        "The same order API as Ch01, but now instrumented to reveal the cost "
        "of short polling. Watch the stats endpoint to see how many requests "
        "are wasted."
    ),
    version="0.2.0",
)

db = DB()

# ---------------------------------------------------------------------------
# Request tracking — the whole point of this chapter
# ---------------------------------------------------------------------------


class PollTracker:
    """Tracks polling statistics to quantify waste.

    Every GET /orders/{id} increments total_polls. If the order status differs
    from the last status the same client saw, it counts as a useful poll.
    Otherwise it is a wasted poll.
    """

    def __init__(self) -> None:
        self.total_polls: int = 0
        self.useful_polls: int = 0
        self.wasted_polls: int = 0
        self.total_bytes_sent: int = 0
        self.start_time: float = time.time()
        # Track last-seen status per (client_ip, order_id) to detect "useful" polls
        self._last_seen: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()
        self._stats_task: asyncio.Task | None = None

    def _efficiency(self) -> float:
        if self.total_polls == 0:
            return 0.0
        return (self.useful_polls / self.total_polls) * 100

    async def record_poll(self, client_id: str, order_id: str, current_status: str, response_bytes: int) -> bool:
        """Record a poll. Returns True if the poll was 'useful' (status changed)."""
        async with self._lock:
            self.total_polls += 1
            self.total_bytes_sent += response_bytes

            key = (client_id, order_id)
            last = self._last_seen.get(key)
            useful = last is None or last != current_status
            self._last_seen[key] = current_status

            if useful:
                self.useful_polls += 1
            else:
                self.wasted_polls += 1

            return useful

    def snapshot(self) -> dict:
        elapsed = time.time() - self.start_time
        rps = self.total_polls / elapsed if elapsed > 0 else 0
        return {
            "elapsed_seconds": round(elapsed, 1),
            "total_polls": self.total_polls,
            "useful_polls": self.useful_polls,
            "wasted_polls": self.wasted_polls,
            "efficiency_pct": round(self._efficiency(), 2),
            "requests_per_second": round(rps, 2),
            "total_bytes_sent": self.total_bytes_sent,
            "total_bytes_sent_human": _human_bytes(self.total_bytes_sent),
        }

    async def _print_stats_loop(self) -> None:
        """Print stats every 10 seconds to stdout so you can watch waste grow."""
        while True:
            await asyncio.sleep(10)
            s = self.snapshot()
            print(
                f"\n[POLL STATS] {s['elapsed_seconds']}s elapsed | "
                f"Total: {s['total_polls']} | "
                f"Useful: {s['useful_polls']} | "
                f"Wasted: {s['wasted_polls']} | "
                f"Efficiency: {s['efficiency_pct']}% | "
                f"Rate: {s['requests_per_second']} req/s | "
                f"Bandwidth: {s['total_bytes_sent_human']}"
            )

    def start_stats_printer(self) -> None:
        if self._stats_task is None:
            self._stats_task = asyncio.create_task(self._print_stats_loop())


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


tracker = PollTracker()

# ---------------------------------------------------------------------------
# Request / response schemas (same as Ch01, reused)
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


class MenuResponse(BaseModel):
    restaurant_id: str
    restaurant_name: str
    items: list[MenuItem]


class AdvanceResponse(BaseModel):
    order_id: str
    previous_status: OrderStatus
    new_status: OrderStatus
    updated_at: float


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def _startup() -> None:
    tracker.start_stats_printer()
    print("=" * 60)
    print("  FoodDash Ch02 — Short Polling Server")
    print("  Tracking poll waste. Watch this console.")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/restaurants/{restaurant_id}/menu", response_model=MenuResponse)
def get_menu(restaurant_id: str) -> MenuResponse:
    """Browse a restaurant's menu. Identical to Ch01."""
    restaurant = db.get_restaurant(restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail=f"Restaurant '{restaurant_id}' not found")
    return MenuResponse(
        restaurant_id=restaurant.id,
        restaurant_name=restaurant.name,
        items=restaurant.menu,
    )


@app.post("/orders", response_model=PlaceOrderResponse, status_code=201)
async def place_order(req: PlaceOrderRequest) -> PlaceOrderResponse:
    """Place a new order. Identical to Ch01."""
    restaurant = db.get_restaurant(req.restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail=f"Restaurant '{req.restaurant_id}' not found")

    menu_by_id: dict[str, MenuItem] = {item.id: item for item in restaurant.menu}
    order_items: list[OrderItem] = []
    for item_id in req.item_ids:
        menu_item = menu_by_id.get(item_id)
        if menu_item is None:
            raise HTTPException(status_code=422, detail=f"Item '{item_id}' not found")
        existing = next((oi for oi in order_items if oi.menu_item.id == item_id), None)
        if existing:
            existing.quantity += 1
        else:
            order_items.append(OrderItem(menu_item=menu_item, quantity=1))

    customer = Customer(name=req.customer_name)
    order = Order(customer=customer, restaurant_id=req.restaurant_id, items=order_items, status=OrderStatus.PLACED)
    await db.place_order(order)

    return PlaceOrderResponse(
        order_id=order.id,
        status=order.status,
        items=[{"name": oi.menu_item.name, "quantity": oi.quantity, "subtotal_cents": oi.subtotal_cents} for oi in order.items],
        total_cents=order.total_cents,
        created_at=order.created_at,
    )


@app.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(order_id: str, request: Request) -> OrderResponse:
    """Get order status — the endpoint that gets hammered by short polling.

    This is functionally identical to Ch01, but now we track every request
    to quantify the waste. The server does the same amount of work regardless
    of whether the status changed. That is the fundamental problem.
    """
    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found")

    response = OrderResponse(
        order_id=order.id,
        customer_name=order.customer.name,
        restaurant_id=order.restaurant_id,
        status=order.status,
        items=[{"name": oi.menu_item.name, "quantity": oi.quantity, "subtotal_cents": oi.subtotal_cents} for oi in order.items],
        total_cents=order.total_cents,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )

    # Estimate response size for bandwidth tracking
    response_json = response.model_dump_json()
    estimated_bytes = len(response_json) + 300  # ~300 bytes for HTTP headers

    client_ip = request.client.host if request.client else "unknown"
    await tracker.record_poll(client_ip, order_id, order.status.value, estimated_bytes)

    return response


@app.post("/orders/{order_id}/advance", response_model=AdvanceResponse)
async def advance_order(order_id: str) -> AdvanceResponse:
    """Advance an order to its next status.

    In the real world, this would be triggered by the restaurant ("food is ready"),
    the driver ("picked up"), or GPS ("arrived at customer"). Here we expose it
    as an endpoint so you can manually trigger status changes while the polling
    client watches.

    Usage:
        curl -X POST http://localhost:8002/orders/{order_id}/advance
    """
    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"Order '{order_id}' not found")

    previous = order.status
    try:
        new_status = order.advance_status()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    print(f"\n>>> ORDER {order_id} advanced: {previous.value} -> {new_status.value}")

    return AdvanceResponse(
        order_id=order.id,
        previous_status=previous,
        new_status=new_status,
        updated_at=order.updated_at,
    )


# ---------------------------------------------------------------------------
# Stats & health
# ---------------------------------------------------------------------------


@app.get("/stats")
def get_stats() -> dict:
    """Live polling statistics. Hit this to see the waste accumulating.

    This is a meta-endpoint — not part of the FoodDash business logic.
    It exists purely to make the educational point: look at that efficiency
    percentage drop as polling continues.
    """
    return tracker.snapshot()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "chapter": "02-short-polling", "timestamp": time.time()}
