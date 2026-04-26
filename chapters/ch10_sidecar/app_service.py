"""FoodDash Order Service -- pure business logic, zero middleware.

This is what a service looks like when cross-cutting concerns are handled
by a sidecar proxy. There is:
  - NO auth verification
  - NO request logging
  - NO rate limiting

The service trusts that the sidecar has already validated the request.
It focuses entirely on business logic: managing orders.

Run:
    uv run python -m chapters.ch10_sidecar.app_service
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from shared.db import DB
from shared.models import Order, OrderItem, Customer, OrderStatus

# ---------------------------------------------------------------------------
# Application setup -- notice: NO middleware at all
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Ch10 -- Order Service (no middleware)",
    description="Pure business logic. Auth, logging, and rate limiting handled by sidecar.",
)
db = DB()

# Seed demo data
_customer = Customer(id="cust_01", name="Jane", address="123 Main St")
_items = [
    OrderItem(menu_item=db.get_restaurant("rest_01").menu[0], quantity=2),
    OrderItem(menu_item=db.get_restaurant("rest_01").menu[1], quantity=1),
]
_demo_order = Order(
    id="order_sc_01",
    customer=_customer,
    restaurant_id="rest_01",
    items=_items,
    status=OrderStatus.PREPARING,
)
db.orders[_demo_order.id] = _demo_order


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class PlaceOrderRequest(BaseModel):
    customer_name: str
    customer_address: str = ""
    restaurant_id: str = "rest_01"
    item_ids: list[str] = ["item_01"]
    quantities: list[int] = [1]


class OrderResponse(BaseModel):
    order_id: str
    status: str
    customer: str
    restaurant: str
    items: int
    total_cents: int


# ---------------------------------------------------------------------------
# Endpoints -- pure business logic
# ---------------------------------------------------------------------------

@app.get("/orders")
async def list_orders():
    """List all orders. No auth check here -- sidecar handles it."""
    orders = []
    for order in db.orders.values():
        orders.append(
            OrderResponse(
                order_id=order.id,
                status=order.status.value,
                customer=order.customer.name,
                restaurant=order.restaurant_id,
                items=len(order.items),
                total_cents=order.total_cents,
            )
        )
    return {"orders": orders}


@app.get("/orders/{order_id}")
async def get_order(order_id: str):
    """Get a single order by ID."""
    order = await db.get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return OrderResponse(
        order_id=order.id,
        status=order.status.value,
        customer=order.customer.name,
        restaurant=order.restaurant_id,
        items=len(order.items),
        total_cents=order.total_cents,
    )


@app.post("/orders")
async def place_order(req: PlaceOrderRequest):
    """Place a new order. The sidecar has already verified auth and rate limits."""
    restaurant = db.get_restaurant(req.restaurant_id)
    if restaurant is None:
        raise HTTPException(status_code=404, detail="Restaurant not found")

    # Build order items from menu
    items = []
    for item_id, qty in zip(req.item_ids, req.quantities):
        menu_item = next((m for m in restaurant.menu if m.id == item_id), None)
        if menu_item is None:
            raise HTTPException(status_code=400, detail=f"Menu item {item_id} not found")
        items.append(OrderItem(menu_item=menu_item, quantity=qty))

    customer = Customer(name=req.customer_name, address=req.customer_address)
    order = Order(
        customer=customer,
        restaurant_id=req.restaurant_id,
        items=items,
    )
    order = await db.place_order(order)

    return OrderResponse(
        order_id=order.id,
        status=order.status.value,
        customer=order.customer.name,
        restaurant=order.restaurant_id,
        items=len(order.items),
        total_cents=order.total_cents,
    )


@app.post("/orders/{order_id}/advance")
async def advance_order(order_id: str):
    """Advance an order to the next status."""
    order = await db.update_order_status(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return OrderResponse(
        order_id=order.id,
        status=order.status.value,
        customer=order.customer.name,
        restaurant=order.restaurant_id,
        items=len(order.items),
        total_cents=order.total_cents,
    )


@app.get("/health")
async def health():
    """Health check -- used by sidecar and orchestrator."""
    return {"status": "ok", "service": "order-service", "port": 8010}


@app.get("/menu")
async def get_menu():
    """Get the restaurant menu."""
    restaurant = db.get_restaurant("rest_01")
    return {
        "restaurant": restaurant.name,
        "items": [
            {"id": item.id, "name": item.name, "price_cents": item.price_cents}
            for item in restaurant.menu
        ],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Chapter 10 -- Order Service (pure business logic)")
    print("=" * 60)
    print("This service has NO auth, NO logging, NO rate limiting.")
    print("Those concerns are handled by the sidecar proxy (port 8011).")
    print(f"\nService listening on http://localhost:8010")
    print("=" * 60 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=8010)
