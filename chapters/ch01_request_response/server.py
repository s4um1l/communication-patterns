"""Chapter 01 — Request-Response: FoodDash Order Server

This is the simplest possible server for our food delivery platform.
Every interaction follows the same pattern:

    Client sends request  -->  Server processes  -->  Server sends response

The client is BLOCKED while waiting. The server does not initiate any
communication — it only speaks when spoken to.

Key things to notice:
    - POST /orders creates a resource and returns 201 Created (not 200 OK)
    - GET endpoints are safe and idempotent — call them a million times, no side effects
    - Every response includes Content-Type: application/json (FastAPI does this automatically)
    - There is no mechanism for the server to notify the client of changes
      (that limitation drives Ch02-Ch05)

Run with:
    uv run uvicorn chapters.ch01_request_response.server:app --port 8001
"""

from __future__ import annotations

import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from shared.db import DB
from shared.models import Customer, MenuItem, Order, OrderItem, OrderStatus

# ---------------------------------------------------------------------------
# App & database
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FoodDash — Ch01 Request-Response",
    description=(
        "A minimal order API demonstrating synchronous request-response. "
        "The client asks, the server answers. Nothing more."
    ),
    version="0.1.0",
)

db = DB()

# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PlaceOrderRequest(BaseModel):
    """What the client sends to place an order.

    Note: we accept item IDs (not full item objects) because the server is the
    source of truth for menu data. Accepting full objects would let a malicious
    client set their own prices.
    """

    customer_name: str = Field(..., min_length=1, examples=["Alice"])
    restaurant_id: str = Field(..., examples=["rest_01"])
    item_ids: list[str] = Field(..., min_length=1, examples=[["item_01", "item_02"]])


class PlaceOrderResponse(BaseModel):
    """What the server returns after creating an order.

    The 201 Created status code tells the client (at the protocol level) that
    a new resource was created. The response body gives the details.
    """

    order_id: str
    status: OrderStatus
    items: list[dict]
    total_cents: int
    created_at: float


class OrderResponse(BaseModel):
    """Full order details returned by GET /orders/{order_id}.

    This is a read-only view. The client cannot modify the order through this
    endpoint — that would require a PUT or PATCH, which we haven't built yet
    because Day 1 at FoodDash is about placing and viewing orders.
    """

    order_id: str
    customer_name: str
    restaurant_id: str
    status: OrderStatus
    items: list[dict]
    total_cents: int
    created_at: float
    updated_at: float


class MenuResponse(BaseModel):
    """The menu for a restaurant. A simple list of items with prices."""

    restaurant_id: str
    restaurant_name: str
    items: list[MenuItem]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/restaurants/{restaurant_id}/menu", response_model=MenuResponse)
def get_menu(restaurant_id: str) -> MenuResponse:
    """Browse a restaurant's menu.

    HTTP semantics demonstrated:
        - GET is safe (no side effects) and idempotent (same result every time)
        - Response uses 200 OK — the standard "here's your data" status
        - This response is cacheable — a CDN could serve it without hitting our server

    In a real system, this would be one of the most-requested endpoints and the
    first candidate for caching (Cache-Control headers, CDN, Redis).
    """
    restaurant = db.get_restaurant(restaurant_id)
    if restaurant is None:
        raise HTTPException(
            status_code=404,
            detail=f"Restaurant '{restaurant_id}' not found",
        )
    return MenuResponse(
        restaurant_id=restaurant.id,
        restaurant_name=restaurant.name,
        items=restaurant.menu,
    )


@app.post("/orders", response_model=PlaceOrderResponse, status_code=201)
async def place_order(req: PlaceOrderRequest) -> PlaceOrderResponse:
    """Place a new order.

    HTTP semantics demonstrated:
        - POST means "create a new resource" — this is NOT idempotent
        - Returns 201 Created (not 200 OK) to signal resource creation
        - The request body is JSON, declared via Content-Type: application/json
        - If the client sends this twice, two orders are created (the double-click problem)

    In production, you would:
        1. Accept an Idempotency-Key header to prevent duplicate orders
        2. Validate payment information
        3. Publish an OrderPlaced event (Ch07: Pub/Sub)

    But this is Day 1 — we keep it simple.
    """
    # Validate: restaurant exists
    restaurant = db.get_restaurant(req.restaurant_id)
    if restaurant is None:
        raise HTTPException(
            status_code=404,
            detail=f"Restaurant '{req.restaurant_id}' not found",
        )

    # Validate: all item IDs exist in this restaurant's menu
    menu_by_id: dict[str, MenuItem] = {item.id: item for item in restaurant.menu}
    order_items: list[OrderItem] = []
    for item_id in req.item_ids:
        menu_item = menu_by_id.get(item_id)
        if menu_item is None:
            raise HTTPException(
                status_code=422,
                detail=f"Item '{item_id}' not found in restaurant '{restaurant.name}'",
            )
        # Check if we already have this item — increment quantity
        existing = next((oi for oi in order_items if oi.menu_item.id == item_id), None)
        if existing:
            existing.quantity += 1
        else:
            order_items.append(OrderItem(menu_item=menu_item, quantity=1))

    # Create the order
    customer = Customer(name=req.customer_name)
    order = Order(
        customer=customer,
        restaurant_id=req.restaurant_id,
        items=order_items,
        status=OrderStatus.PLACED,
    )
    await db.place_order(order)

    return PlaceOrderResponse(
        order_id=order.id,
        status=order.status,
        items=[
            {
                "name": oi.menu_item.name,
                "quantity": oi.quantity,
                "subtotal_cents": oi.subtotal_cents,
            }
            for oi in order.items
        ],
        total_cents=order.total_cents,
        created_at=order.created_at,
    )


@app.get("/orders/{order_id}", response_model=OrderResponse)
async def get_order(order_id: str) -> OrderResponse:
    """Get the current state of an order.

    HTTP semantics demonstrated:
        - GET is safe and idempotent — perfect for status checks
        - Returns 200 OK with the current snapshot of the order
        - This is a POINT-IN-TIME read — it tells you the state NOW
        - It CANNOT tell you when the state will change (that's Ch02's problem)

    Notice what's missing: there is no way for the server to say "hey, your
    order just moved to 'preparing'!" The client must ask again. And again.
    This is the fundamental limitation that drives the rest of this course.
    """
    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(
            status_code=404,
            detail=f"Order '{order_id}' not found",
        )
    return OrderResponse(
        order_id=order.id,
        customer_name=order.customer.name,
        restaurant_id=order.restaurant_id,
        status=order.status,
        items=[
            {
                "name": oi.menu_item.name,
                "quantity": oi.quantity,
                "subtotal_cents": oi.subtotal_cents,
            }
            for oi in order.items
        ],
        total_cents=order.total_cents,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    """Basic health check. Also useful for demonstrating the simplest possible
    request-response: no parameters, no body, just a question and an answer."""
    return {"status": "ok", "chapter": "01-request-response", "timestamp": time.time()}
