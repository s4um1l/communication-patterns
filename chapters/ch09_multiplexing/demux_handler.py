"""Server-side demultiplexer -- one WebSocket, three stream handlers.

This FastAPI server accepts a single WebSocket connection per client and
demultiplexes incoming frames to the correct handler based on stream_type:
  - CHAT (1):         Echo-style chat handler
  - ORDER_STATUS (2): Order lookup using the shared DB
  - LOCATION (3):     Driver location update handler

Run:
    uv run python -m chapters.ch09_multiplexing.demux_handler
"""

from __future__ import annotations

import time
import uvicorn
from fastapi import FastAPI, WebSocket

from shared.db import DB
from shared.models import Order, OrderItem, Customer, OrderStatus
from chapters.ch09_multiplexing.mux_protocol import (
    Demultiplexer,
    Frame,
    StreamType,
    STREAM_NAMES,
)

# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

app = FastAPI(title="Ch09 -- Multiplexing Demux Server")
db = DB()

# Seed a demo order so status checks have something to find
_demo_customer = Customer(id="cust_01", name="Jane", address="123 Main St")
_demo_items = [
    OrderItem(
        menu_item=db.get_restaurant("rest_01").menu[0],  # Classic Burger
        quantity=2,
    )
]
_demo_order = Order(
    id="order_mux_01",
    customer=_demo_customer,
    restaurant_id="rest_01",
    items=_demo_items,
    status=OrderStatus.PREPARING,
)
db.orders[_demo_order.id] = _demo_order


# ---------------------------------------------------------------------------
# Stream handlers -- one per stream type
# ---------------------------------------------------------------------------

async def handle_chat(frame: Frame) -> dict:
    """Handle CHAT frames.

    In a real system this would route to a chat service. Here we echo
    back with a server timestamp, simulating a driver response.
    """
    payload = frame.payload_json()
    msg = payload.get("msg", "")
    print(f"  [CHAT] stream={frame.stream_id} | {msg}")
    return {
        "msg": f"Driver says: Got it! '{msg}'",
        "server_ts": time.time(),
    }


async def handle_order_status(frame: Frame) -> dict:
    """Handle ORDER_STATUS frames.

    Looks up the order in the DB and returns current status. If the order
    is not found, returns an error payload.
    """
    payload = frame.payload_json()
    order_id = payload.get("order_id", "")
    action = payload.get("action", "check")
    print(f"  [ORDER] stream={frame.stream_id} | {action} {order_id}")

    order = await db.get_order(order_id)
    if order is None:
        return {"order_id": order_id, "error": "Order not found"}

    if action == "advance":
        order = await db.update_order_status(order_id)
        return {
            "order_id": order_id,
            "status": order.status.value,
            "action": "advanced",
        }

    return {
        "order_id": order_id,
        "status": order.status.value,
        "restaurant": order.restaurant_id,
        "items": len(order.items),
    }


async def handle_location(frame: Frame) -> dict:
    """Handle LOCATION frames.

    Updates a driver's position. In production this would write to a
    geospatial index for real-time tracking.
    """
    payload = frame.payload_json()
    driver_id = payload.get("driver_id", "drv_01")
    lat = payload.get("lat", 0.0)
    lng = payload.get("lng", 0.0)
    print(f"  [LOCATION] stream={frame.stream_id} | {driver_id} @ ({lat}, {lng})")

    driver = db.drivers.get(driver_id)
    if driver:
        driver.latitude = lat
        driver.longitude = lng
        return {
            "driver_id": driver_id,
            "lat": driver.latitude,
            "lng": driver.longitude,
            "ack": True,
        }
    return {"driver_id": driver_id, "error": "Driver not found"}


# ---------------------------------------------------------------------------
# WebSocket endpoint -- single connection, all streams
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def multiplexed_endpoint(ws: WebSocket):
    """Accept a single WebSocket and demux frames to handlers.

    This is the key insight: one connection replaces three. The client
    sends frames tagged with stream_type, and the demuxer routes each
    frame to the correct handler.
    """
    await ws.accept()
    client = ws.client
    print(f"\n{'='*60}")
    print(f"[SERVER] Client connected: {client}")
    print(f"[SERVER] Single WebSocket handling CHAT + ORDER + LOCATION")
    print(f"{'='*60}\n")

    demux = Demultiplexer(ws=ws)
    demux.register(StreamType.CHAT, handle_chat)
    demux.register(StreamType.ORDER_STATUS, handle_order_status)
    demux.register(StreamType.LOCATION, handle_location)

    await demux.run()

    print(f"\n[SERVER] Client disconnected. Frames processed: {demux.frames_received}")


# ---------------------------------------------------------------------------
# Health check (for docker-compose / sidecar scenarios)
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "chapter": "ch09_multiplexing", "port": 8009}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("Chapter 09 -- Multiplexing / Demultiplexing Server")
    print("=" * 60)
    print("One WebSocket connection handles three logical streams:")
    print(f"  Stream type 1: CHAT         (customer <-> driver)")
    print(f"  Stream type 2: ORDER_STATUS (order queries)")
    print(f"  Stream type 3: LOCATION     (driver GPS updates)")
    print(f"\nListening on ws://localhost:8009/ws")
    print("=" * 60 + "\n")

    uvicorn.run(app, host="0.0.0.0", port=8009)
